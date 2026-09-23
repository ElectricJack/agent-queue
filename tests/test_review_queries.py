"""Document review: schema, migration ``a00000000014`` and the query layer.

Covers Task 1 of the document-review plan
(``vault/projects/agent-queue/specs/2026-09-21-document-review-design.md``
§3.2-§3.3):

* ``insert_review`` / ``get_review`` round-trip, ``list_reviews`` filters.
* ``transition_review`` is a compare-and-set on ``state`` and
  ``current_revision``.
* ``list_reviews(task_id=…)`` finds a task's reviews as ``author`` or as
  ``waiting`` on the review's gate.
* ``attach_gate_waiters`` blocks its waiters and is idempotent.
* No foreign key to ``tasks``: deleting the author task leaves the review.
* ``generate_review_id`` follows task-id style and never collides.
* The migration upgrades a pre-revision schema, is idempotent, and downgrades.
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import GATE_TYPES
from src.models import Project, Task, TaskStatus
from src.task_names import ADJECTIVES, NOUNS
from tests.db_fixtures import lease_dsn

PROJECT = "p-rev"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def db():
    database = Database(lease_dsn("reviews.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="reviews"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title=tid, description=tid, status=status, **kw)
    )
    return tid


def _review(review_id="rev-bright-harbor", **overrides) -> dict:
    now = time.time()
    row = {
        "id": review_id,
        "project_id": PROJECT,
        "author_task_id": "author-task",
        "kind": "spec",
        "title": "Document review",
        "vault_path": f"projects/{PROJECT}/specs/2026-09-21-{review_id}.md",
        "current_revision": 1,
        "state": "in_review",
        "gate_id": None,
        "decider": "user",
        "decided_by": None,
        "decided_at": None,
        "decision_note": None,
        "notified_revision": 0,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _revision(review_id="rev-bright-harbor", revision=1, content="# Body\n", **overrides) -> dict:
    row = {
        "review_id": review_id,
        "revision": revision,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "submitted_by": "session:author",
        "submitted_task_id": "author-task",
        "changes_note": None,
        "submitted_at": time.time(),
        "responder_class": None,
        "responder_profile": None,
        "responder_profile_source": None,
    }
    row.update(overrides)
    return row


async def _submit(db, review_id="rev-bright-harbor", *, with_gate=False, **overrides) -> dict:
    """Insert a review and its first revision; optionally with its ``review`` gate."""
    async with db.immediate() as conn:
        if with_gate:
            gate_id, _ = await db.create_gate(
                overrides.get("project_id", PROJECT),
                "review",
                "Document review",
                await_id=review_id,
                conn=conn,
            )
            overrides["gate_id"] = gate_id
        review = _review(review_id, **overrides)
        await db.insert_review(review=review, revision=_revision(review_id), conn=conn)
    return review


# ── insert / get / list ────────────────────────────────────────────────────


class TestInsertAndRead:
    async def test_insert_and_get_round_trip_every_column(self, db):
        review = _review(
            decided_by="human:local-operator",
            decided_at=123.5,
            decision_note="looks fine",
            notified_revision=1,
            decider="user_or_supervisor",
            kind="plan",
        )
        async with db.immediate() as conn:
            await db.insert_review(review=review, revision=_revision(), conn=conn)

        assert await db.get_review(review["id"]) == review

    async def test_get_missing_review_is_none(self, db):
        assert await db.get_review("rev-nope-none") is None

    async def test_insert_stores_the_first_revision(self, db):
        await _submit(db)
        revision = await db.get_review_revision("rev-bright-harbor", 1)
        assert revision == _revision() | {"submitted_at": revision["submitted_at"]}
        assert await db.get_review_revision("rev-bright-harbor", 2) is None

    async def test_list_reviews_filters(self, db):
        await db.create_project(Project(id="p-other", name="other"))
        await _submit(db, "rev-a-one")
        await _submit(db, "rev-b-two", state="changes_requested", kind="plan")
        await _submit(db, "rev-c-three", project_id="p-other")

        in_review = await db.list_reviews(project_id=PROJECT, state="in_review")
        assert [r["id"] for r in in_review] == ["rev-a-one"]
        assert {r["id"] for r in await db.list_reviews(project_id=PROJECT)} == {
            "rev-a-one",
            "rev-b-two",
        }
        assert [r["id"] for r in await db.list_reviews(kind="plan")] == ["rev-b-two"]
        assert {r["id"] for r in await db.list_reviews()} == {
            "rev-a-one",
            "rev-b-two",
            "rev-c-three",
        }
        # Without a task filter the rows carry no relation.
        assert all("relation" not in r for r in await db.list_reviews())

    async def test_list_reviews_newest_update_first(self, db):
        await _submit(db, "rev-a-one", updated_at=100.0)
        await _submit(db, "rev-b-two", updated_at=300.0)
        await _submit(db, "rev-c-three", updated_at=200.0)
        assert [r["id"] for r in await db.list_reviews()] == [
            "rev-b-two",
            "rev-c-three",
            "rev-a-one",
        ]

    async def test_vault_path_is_unique(self, db):
        await _submit(db, "rev-a-one", vault_path="projects/p/specs/x.md")
        async with db.immediate() as conn:
            assert await db.review_vault_path_taken("projects/p/specs/x.md", conn=conn)
            assert not await db.review_vault_path_taken("projects/p/specs/y.md", conn=conn)
        with pytest.raises(IntegrityError):
            await _submit(db, "rev-b-two", vault_path="projects/p/specs/x.md")

    async def test_check_constraints_reject_unknown_values(self, db):
        for column, value in (("kind", "memo"), ("state", "open"), ("decider", "anyone")):
            with pytest.raises(IntegrityError, match=f"ck_doc_reviews_{column}"):
                await _submit(db, "rev-bad-value", **{column: value})


# ── transitions ────────────────────────────────────────────────────────────


class TestTransition:
    async def test_transition_applies_when_state_and_revision_match(self, db):
        await _submit(db)
        async with db.immediate() as conn:
            ok = await db.transition_review(
                "rev-bright-harbor",
                from_states={"in_review"},
                expected_revision=1,
                values={"state": "changes_requested", "decided_by": "human:local-operator"},
                conn=conn,
            )
        assert ok is True
        row = await db.get_review("rev-bright-harbor")
        assert row["state"] == "changes_requested"
        assert row["decided_by"] == "human:local-operator"

    @pytest.mark.parametrize(
        ("from_states", "expected_revision"),
        [({"changes_requested"}, 1), ({"in_review"}, 2), ({"approved", "withdrawn"}, 1)],
    )
    async def test_transition_refuses_a_stale_state_or_revision(
        self, db, from_states, expected_revision
    ):
        before = await _submit(db)
        async with db.immediate() as conn:
            ok = await db.transition_review(
                "rev-bright-harbor",
                from_states=from_states,
                expected_revision=expected_revision,
                values={"state": "approved", "current_revision": 7},
                conn=conn,
            )
        assert ok is False
        assert await db.get_review("rev-bright-harbor") == before

    async def test_transition_of_a_missing_review_is_false(self, db):
        async with db.immediate() as conn:
            assert not await db.transition_review(
                "rev-nope-none",
                from_states={"in_review"},
                expected_revision=1,
                values={"state": "approved"},
                conn=conn,
            )


# ── revisions ──────────────────────────────────────────────────────────────


class TestRevisions:
    async def test_revisions_list_ascending_without_content(self, db):
        await _submit(db)
        async with db.immediate() as conn:
            await db.insert_review_revision(
                revision=_revision(revision=2, content="# Body v2\n", changes_note="tightened"),
                conn=conn,
            )
        revisions = await db.list_review_revisions("rev-bright-harbor")
        assert [r["revision"] for r in revisions] == [1, 2]
        assert all("content" not in r for r in revisions)
        assert revisions[1]["changes_note"] == "tightened"
        assert revisions[1]["content_sha256"] == hashlib.sha256(b"# Body v2\n").hexdigest()
        full = await db.get_review_revision("rev-bright-harbor", 2)
        assert full["content"] == "# Body v2\n"

    async def test_duplicate_revision_number_is_refused(self, db):
        await _submit(db)
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await db.insert_review_revision(revision=_revision(revision=1), conn=conn)


# ── comments ───────────────────────────────────────────────────────────────


def _comment(cid, *, created_at, **overrides) -> dict:
    row = {
        "id": cid,
        "review_id": "rev-bright-harbor",
        "revision": 1,
        "quote": "the exact text",
        "heading_path": ["Document review", "3. Where things live"],
        "body": f"comment {cid}",
        "author": "human:local-operator",
        "resolved_in_revision": None,
        "created_at": created_at,
    }
    row.update(overrides)
    return row


class TestComments:
    async def test_comments_round_trip_in_creation_order(self, db):
        await _submit(db)
        second = _comment("c-2", created_at=200.0, quote=None, heading_path=[])
        first = _comment("c-1", created_at=100.0)
        await db.insert_review_comment(second)
        await db.insert_review_comment(first)
        rows = await db.list_review_comments("rev-bright-harbor")
        # Ordered by the creation clock (created_at), which differs here, not
        # by the order the inserts ran in.
        assert [c["id"] for c in rows] == ["c-1", "c-2"]
        assert [c["created_at"] for c in rows] == [100.0, 200.0]
        # created_seq records insertion order (c-2 went in first), independent
        # of the clock: it is the tiebreak a coarse clock cannot provide.
        assert [c["created_seq"] for c in rows] == [2, 1]
        assert rows[0] == {**first, "created_seq": 2}
        assert rows[1] == {**second, "created_seq": 1}

    async def test_tied_timestamps_are_ordered_by_creation_not_id(self, db):
        # A coarse or frozen clock can hand two comments the same created_at.
        # The list must then read in the order they were made (created_seq),
        # not in the order of their random ids: c-b's id sorts before c-a's,
        # so the old created_at, id ordering would have returned them reversed.
        await _submit(db)
        a = _comment("c-b", created_at=100.0)
        b = _comment("c-a", created_at=100.0)
        await db.insert_review_comment(a)
        await db.insert_review_comment(b)
        rows = await db.list_review_comments("rev-bright-harbor")
        assert [c["id"] for c in rows] == ["c-b", "c-a"]
        assert [c["created_at"] for c in rows] == [100.0, 100.0]
        assert [c["created_seq"] for c in rows] == [1, 2]
        assert rows[0] == {**a, "created_seq": 1}
        assert rows[1] == {**b, "created_seq": 2}

    async def test_comment_body_length_is_checked(self, db):
        await _submit(db)
        for body in ("", "x" * 16001):
            with pytest.raises(IntegrityError, match="ck_doc_review_comments_body"):
                await db.insert_review_comment(_comment("c-bad", created_at=1.0, body=body))
        await db.insert_review_comment(_comment("c-max", created_at=1.0, body="x" * 16000))
        # The limit counts characters, not UTF-8 bytes.
        await db.insert_review_comment(_comment("c-wide", created_at=2.0, body="é" * 16000))

    async def test_resolve_marks_only_named_open_comments_of_this_review(self, db):
        await _submit(db)
        await _submit(db, "rev-other-review")
        await db.insert_review_comment(_comment("c-1", created_at=1.0))
        await db.insert_review_comment(_comment("c-2", created_at=2.0))
        await db.insert_review_comment(_comment("c-3", created_at=3.0, resolved_in_revision=1))
        await db.insert_review_comment(
            _comment("c-4", created_at=4.0, review_id="rev-other-review")
        )
        async with db.immediate() as conn:
            count = await db.resolve_review_comments(
                "rev-bright-harbor", ["c-1", "c-3", "c-4", "c-missing"], 2, conn=conn
            )
            assert await db.resolve_review_comments("rev-bright-harbor", [], 2, conn=conn) == 0
        assert count == 1
        resolved = {
            c["id"]: c["resolved_in_revision"]
            for c in await db.list_review_comments("rev-bright-harbor")
        }
        assert resolved == {"c-1": 2, "c-2": None, "c-3": 1}
        other = await db.list_review_comments("rev-other-review")
        assert other[0]["resolved_in_revision"] is None


# ── Discord outbox ─────────────────────────────────────────────────────────


class TestNotificationOutbox:
    async def test_pending_until_marked_and_marking_is_monotonic(self, db):
        await _submit(db, "rev-a-one")
        await _submit(db, "rev-b-two", current_revision=3, notified_revision=3)
        assert [r["id"] for r in await db.reviews_pending_notification()] == ["rev-a-one"]

        await db.mark_review_notified("rev-a-one", 1)
        assert await db.reviews_pending_notification() == []

        # A late mark for an older revision never moves the outbox backwards.
        await db.mark_review_notified("rev-b-two", 2)
        assert (await db.get_review("rev-b-two"))["notified_revision"] == 3


# ── gates ──────────────────────────────────────────────────────────────────


class TestReviewGate:
    def test_review_is_a_gate_type_everywhere(self):
        from src.tools.definitions import _FALLBACK_INPUT_SCHEMAS

        assert "review" in GATE_TYPES
        enum = _FALLBACK_INPUT_SCHEMAS["gate_list"]["properties"]["gate_type"]["enum"]
        assert enum == list(GATE_TYPES)

    async def test_attach_gate_waiters_blocks_and_is_idempotent(self, db):
        await mktask(db, "impl")
        review = await _submit(db, with_gate=True)
        assert (await db.get_task("impl")).is_blocked is False

        async with db.immediate() as conn:
            flipped = await db.attach_gate_waiters(review["gate_id"], ["impl"], conn=conn)
        assert flipped == {"impl"}
        assert (await db.get_task("impl")).is_blocked is True

        async with db.immediate() as conn:
            again = await db.attach_gate_waiters(review["gate_id"], ["impl", "impl"], conn=conn)
            assert await db.attach_gate_waiters(review["gate_id"], [], conn=conn) == set()
        assert again == set()
        assert await db.get_gate_waiters(review["gate_id"]) == {"impl"}

    async def test_list_reviews_by_task_reports_author_and_waiting(self, db):
        await mktask(db, "author-task", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "impl")
        await mktask(db, "bystander")
        review = await _submit(db, with_gate=True)
        async with db.immediate() as conn:
            await db.attach_gate_waiters(review["gate_id"], ["impl"], conn=conn)

        authored = await db.list_reviews(task_id="author-task")
        assert [(r["id"], r["relation"]) for r in authored] == [
            ("rev-bright-harbor", "author")
        ]
        waiting = await db.list_reviews(task_id="impl")
        assert [(r["id"], r["relation"]) for r in waiting] == [("rev-bright-harbor", "waiting")]
        assert await db.list_reviews(task_id="bystander") == []
        # The task filter composes with the others.
        assert await db.list_reviews(task_id="impl", state="approved") == []

    async def test_author_that_also_waits_is_reported_once_as_author(self, db):
        await mktask(db, "author-task")
        review = await _submit(db, with_gate=True)
        async with db.immediate() as conn:
            await db.attach_gate_waiters(review["gate_id"], ["author-task"], conn=conn)
        rows = await db.list_reviews(task_id="author-task")
        assert [(r["id"], r["relation"]) for r in rows] == [("rev-bright-harbor", "author")]


# ── soft task references ───────────────────────────────────────────────────


class TestNoTaskForeignKey:
    async def test_deleting_the_author_task_keeps_the_review(self, db):
        await mktask(db, "author-task", status=TaskStatus.IN_PROGRESS)
        await _submit(db)
        await db.insert_review_comment(_comment("c-1", created_at=1.0))

        await db.delete_task("author-task")

        assert await db.get_task("author-task") is None
        review = await db.get_review("rev-bright-harbor")
        assert review["author_task_id"] == "author-task"
        revision = await db.get_review_revision("rev-bright-harbor", 1)
        assert revision["submitted_task_id"] == "author-task"

    async def test_review_tables_reference_no_task_table(self, db):
        async with db._engine.connect() as conn:
            targets = set(
                (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT confrelid::regclass::text FROM pg_constraint "
                            "WHERE contype = 'f' AND conrelid::regclass::text IN "
                            "('doc_reviews', 'doc_review_revisions', 'doc_review_comments')"
                        )
                    )
                ).scalars()
            )
        assert targets == {"doc_reviews"}


# ── ids ────────────────────────────────────────────────────────────────────


class TestReviewId:
    async def test_id_follows_task_id_style(self, db):
        review_id = await db.generate_review_id()
        prefix, adjective, noun = review_id.split("-", 2)
        assert prefix == "rev"
        assert adjective in ADJECTIVES
        assert noun in NOUNS

    async def test_id_never_reuses_an_existing_one(self, db, monkeypatch):
        import random

        from src.database.queries import review_queries

        await _submit(db, f"rev-{ADJECTIVES[0]}-{NOUNS[0]}")
        # Every plain pick collides, so the generator must fall back to a suffix.
        monkeypatch.setattr(review_queries.random, "choice", lambda seq: seq[0])
        monkeypatch.setattr(review_queries.random, "randint", random.Random(7).randint)
        review_id = await db.generate_review_id()
        assert review_id.startswith(f"rev-{ADJECTIVES[0]}-{NOUNS[0]}-")
        assert 10 <= int(review_id.rsplit("-", 1)[1]) <= 99


# ── project delegation default ─────────────────────────────────────────────


class TestProjectDelegation:
    async def test_review_delegate_to_round_trips(self, db):
        assert (await db.get_project(PROJECT)).review_delegate_to is None
        await db.create_project(Project(id="p-del", name="d", review_delegate_to="supervisor"))
        assert (await db.get_project("p-del")).review_delegate_to == "supervisor"
        await db.update_project("p-del", review_delegate_to="user")
        assert (await db.get_project("p-del")).review_delegate_to == "user"

    async def test_review_delegate_to_is_checked(self, db):
        with pytest.raises(IntegrityError, match="ck_projects_review_delegate_to"):
            await db.update_project(PROJECT, review_delegate_to="anyone")


# ── migration a00000000014 ─────────────────────────────────────────────────


def _load_migration():
    path = REPO_ROOT / "migrations" / "versions" / "a00000000014_document_reviews.py"
    spec = importlib.util.spec_from_file_location("a00000000014", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(migration, *steps: str):
    def _apply(sync_conn) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        with Operations.context(MigrationContext.configure(sync_conn)):
            for step in steps:
                getattr(migration, step)()

    return _apply


_PRE_REVISION_GATE_CHECK = (
    "gate_type IN ('human', 'timer', 'pr-merged', 'ci-run', 'event', 'task', 'routing')"
)


async def _shape(conn) -> dict:
    tables = set(
        (
            await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
        ).scalars()
    )
    columns = set(
        (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'projects'"
                )
            )
        ).scalars()
    )
    checks = dict(
        (
            await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname IN ('ck_gates_type', 'ck_projects_review_delegate_to')"
                )
            )
        ).all()
    )
    return {
        "review_tables": tables & {"doc_reviews", "doc_review_revisions", "doc_review_comments"},
        "delegate_column": "review_delegate_to" in columns,
        "delegate_check": "ck_projects_review_delegate_to" in checks,
        "review_gate_type": "'review'" in checks.get("ck_gates_type", ""),
    }


_HEAD = {
    "review_tables": {"doc_reviews", "doc_review_revisions", "doc_review_comments"},
    "delegate_column": True,
    "delegate_check": True,
    "review_gate_type": True,
}
_BEFORE = {
    "review_tables": set(),
    "delegate_column": False,
    "delegate_check": False,
    "review_gate_type": False,
}


class TestMigration:
    """Every case runs in one transaction that is rolled back (PostgreSQL DDL is
    transactional), so the leased database leaves the test at head whatever
    happens."""

    async def test_upgrade_from_the_pre_revision_shape_is_idempotent(self, db):
        migration = _load_migration()
        async with db._engine.connect() as conn:
            trans = await conn.begin()
            try:
                # Rewind to a00000000013: no review tables, no delegation
                # column, and a gate-type CHECK without 'review'.
                await conn.execute(
                    text("DROP TABLE doc_review_comments, doc_review_revisions, doc_reviews")
                )
                await conn.execute(text("ALTER TABLE projects DROP COLUMN review_delegate_to"))
                await conn.execute(text("ALTER TABLE gates DROP CONSTRAINT ck_gates_type"))
                await conn.execute(
                    text(
                        "ALTER TABLE gates ADD CONSTRAINT ck_gates_type "
                        f"CHECK ({_PRE_REVISION_GATE_CHECK})"
                    )
                )
                assert await _shape(conn) == _BEFORE

                await conn.run_sync(_run(migration, "upgrade", "upgrade"))
                assert await _shape(conn) == _HEAD

                # The new gate type is accepted, an unknown one still is not.
                await conn.execute(
                    text(
                        "INSERT INTO gates (id, project_id, gate_type, title, created_at) "
                        f"VALUES ('gate-rev', '{PROJECT}', 'review', 't', 0)"
                    )
                )
                with pytest.raises(IntegrityError, match="ck_gates_type"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO gates (id, project_id, gate_type, title, created_at) "
                                f"VALUES ('gate-bad', '{PROJECT}', 'memo', 't', 0)"
                            )
                        )
            finally:
                await trans.rollback()

    async def test_upgrade_at_head_is_a_no_op(self, db):
        migration = _load_migration()
        async with db._engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_run(migration, "upgrade"))
                assert await _shape(conn) == _HEAD
            finally:
                await trans.rollback()

    async def test_downgrade_removes_reviews_and_their_gates(self, db):
        await mktask(db, "impl")
        review = await _submit(db, with_gate=True)
        async with db.immediate() as conn:
            await db.attach_gate_waiters(review["gate_id"], ["impl"], conn=conn)
        migration = _load_migration()
        async with db._engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_run(migration, "downgrade"))
                assert await _shape(conn) == _BEFORE
                remaining = (
                    await conn.execute(
                        text("SELECT count(*) FROM gates WHERE gate_type = 'review'")
                    )
                ).scalar()
                waiters = (await conn.execute(text("SELECT count(*) FROM task_gates"))).scalar()
                assert (remaining, waiters) == (0, 0)
                # The released waiter is not left blocked on a gate that is gone.
                blocked = (
                    await conn.execute(text("SELECT is_blocked FROM tasks WHERE id = 'impl'"))
                ).scalar()
                assert not blocked

                await conn.run_sync(_run(migration, "upgrade"))
                assert await _shape(conn) == _HEAD
            finally:
                await trans.rollback()
