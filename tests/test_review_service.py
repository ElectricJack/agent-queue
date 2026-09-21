"""Document review: ``ReviewService`` over the real database and a temp vault.

Covers Task 2 of the document-review plan
(``vault/projects/agent-queue/specs/2026-09-21-document-review-design.md``
§3-§7, §10): submit, revise, decide, comment, withdraw, delegate,
import-edits and show, each asserting the review row, the vault file and the
hook calls.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.reviews.diff import block_diff
from src.reviews.service import MAX_CONTENT_BYTES, ReviewError, ReviewHooks, ReviewService
from src.reviews.vault import body_sha256, split_frontmatter
from tests.db_fixtures import lease_dsn

PROJECT = "p"
NOW = 1_790_000_000.0
DATE = time.strftime("%Y-%m-%d", time.localtime(NOW))
OPERATOR = "human:local-operator"
DOC = "# Doc\n\nIntro paragraph.\n\n## Scope\n\nBody.\n"


class RecordingHooks:
    def __init__(self, db):
        self.db, self.events, self.resolved, self.changes = db, [], [], []

    async def emit(self, event_type, payload):
        self.events.append((event_type, payload))

    async def resolve_gate(self, gate_id, resolved_by, resolution):
        self.resolved.append((gate_id, resolved_by, resolution))
        return await self.db.resolve_gate(gate_id, resolved_by=resolved_by, resolution=resolution)

    async def changes_requested(self, review, feedback):
        self.changes.append((review["id"], feedback))

    def of(self, event_type: str) -> list[dict]:
        return [payload for kind, payload in self.events if kind == event_type]


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def db():
    database = Database(lease_dsn("review_service.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="p"))
    yield database
    await database.close()


@pytest.fixture
def hooks(db):
    return RecordingHooks(db)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(db, hooks, clock, tmp_path):
    return ReviewService(
        db,
        tmp_path,
        ReviewHooks(
            emit=hooks.emit,
            resolve_gate=hooks.resolve_gate,
            changes_requested=hooks.changes_requested,
        ),
        clock=clock,
    )


async def mktask(db, tid, status=TaskStatus.READY):
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title=f"title {tid}", description=tid, status=status)
    )
    return tid


async def submit(svc, *, title="Document review", content=DOC, kind="spec", author="author"):
    return await svc.submit(
        project_id=PROJECT,
        author_task_id=author,
        kind=kind,
        title=title,
        content=content,
        submitted_by="session:worker",
    )


async def revise(svc, review_id, content, *, note="changed", resolves=()):
    return await svc.revise(
        review_id=review_id,
        content=content,
        changes_note=note,
        resolves=list(resolves),
        submitted_by="session:worker",
        submitted_task_id="author",
    )


async def approve(svc, review_id, revision=1, note="ok"):
    return await svc.decide(
        review_id=review_id, revision=revision, approve=True, note=note, decided_by=OPERATOR
    )


async def request_changes(svc, review_id, revision=1, note="Tighten scope."):
    return await svc.decide(
        review_id=review_id, revision=revision, approve=False, note=note, decided_by=OPERATOR
    )


async def attach(db, gate_id, *task_ids):
    async with db.immediate() as conn:
        await db.attach_gate_waiters(gate_id, task_ids, conn=conn)


def read_vault(tmp_path: Path, vault_path: str) -> tuple[dict, str]:
    fm, body = split_frontmatter((tmp_path / vault_path).read_bytes().decode("utf-8"))
    assert fm is not None
    return dict(YAML(typ="safe").load(fm)), body


async def raises(code: str, coro) -> ReviewError:
    with pytest.raises(ReviewError) as info:
        await coro
    assert info.value.code == code, info.value.message
    assert info.value.message
    return info.value


# ── 1. submit ────────────────────────────────────────────────────────────────


async def test_submit_writes_the_vault_copy_opens_a_review_gate_and_emits(svc, db, hooks, tmp_path):
    result = await submit(
        svc,
        title="Document review: in the Dashboard!",
        content="---\nsubmitted: frontmatter\n---\n" + DOC,
    )
    review_id = result["review_id"]
    assert review_id.startswith("rev-")
    assert result["revision"] == 1
    assert (
        result["vault_path"]
        == f"projects/{PROJECT}/specs/{DATE}-document-review-in-the-dashboard.md"
    )

    fm, body = read_vault(tmp_path, result["vault_path"])
    assert body == DOC
    assert fm == {
        "title": "Document review: in the Dashboard!",
        "status": "in_review",
        "kind": "spec",
        "review": review_id,
        "revision": 1,
        "project": PROJECT,
        "author_task": "author",
        "date": fm["date"],
    }
    assert str(fm["date"]) == DATE

    review = await db.get_review(review_id)
    assert review["state"] == "in_review"
    assert review["current_revision"] == 1
    assert review["decider"] == "user"
    assert review["gate_id"] == result["gate_id"]
    assert review["created_at"] == review["updated_at"] == NOW
    revision = await db.get_review_revision(review_id, 1)
    assert revision["content"] == DOC
    assert revision["content_sha256"] == body_sha256(DOC)
    assert revision["submitted_by"] == "session:worker"
    assert revision["submitted_task_id"] == "author"

    gate = await db.get_gate(result["gate_id"])
    assert (gate["gate_type"], gate["await_id"], gate["status"]) == ("review", review_id, "open")
    assert gate["project_id"] == PROJECT

    [event] = hooks.of("review.submitted")
    assert event["review_id"] == review_id
    assert event["project_id"] == PROJECT
    assert event["revision"] == 1
    assert event["kind"] == "spec"
    assert event["author_task_id"] == "author"
    assert event["vault_path"] == result["vault_path"]


async def test_submit_records_the_decider_and_a_plan_goes_to_plans(svc, db, tmp_path):
    result = await svc.submit(
        project_id=PROJECT,
        author_task_id=None,
        kind="plan",
        title="Plan",
        content="# Plan\n",
        submitted_by=OPERATOR,
        decider="user_or_supervisor",
    )
    assert result["vault_path"] == f"projects/{PROJECT}/plans/{DATE}-plan.md"
    review = await db.get_review(result["review_id"])
    assert review["decider"] == "user_or_supervisor"
    assert review["author_task_id"] is None
    assert read_vault(tmp_path, result["vault_path"])[0]["author_task"] == ""


async def test_a_crlf_body_is_stored_byte_exact_and_does_not_read_as_diverged(svc, db):
    content = "# Doc\r\n\r\nBody.\r\n"
    result = await submit(svc, content=content)
    shown = await svc.show(review_id=result["review_id"])
    assert shown["revision"]["content"] == content
    assert shown["vault_state"] == "ok"


# ── 2. submit validation ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"kind": "memo"}, "bad_kind"),
        ({"title": ""}, "bad_title"),
        ({"title": "   "}, "bad_title"),
        ({"title": "two\nlines"}, "bad_title"),
        ({"title": "x" * 201}, "bad_title"),
        ({"content": "  \n\t\n"}, "empty"),
        ({"content": "---\ntitle: only frontmatter\n---\n  \n"}, "empty"),
        ({"content": "x" * (MAX_CONTENT_BYTES + 1)}, "too_large"),
        ({"content": "é" * (MAX_CONTENT_BYTES // 2 + 1)}, "too_large"),
        ({"content": "# ok \ud800\n"}, "not_utf8"),
    ],
)
async def test_submit_validation(svc, db, hooks, tmp_path, overrides, code):
    kwargs = {"title": "Doc", "content": DOC, "kind": "spec"} | overrides
    await raises(code, submit(svc, **kwargs))
    assert await db.list_reviews(project_id=PROJECT) == []
    assert not (tmp_path / "projects").exists()
    assert hooks.events == []


async def test_content_at_the_limit_is_accepted(svc):
    result = await submit(svc, content="x" * MAX_CONTENT_BYTES)
    assert result["revision"] == 1


# ── 3. path collisions ──────────────────────────────────────────────────────


async def test_path_collisions_get_a_numeric_suffix(svc, tmp_path):
    specs = f"projects/{PROJECT}/specs"
    first = await submit(svc, title="Same")
    second = await submit(svc, title="Same")
    assert first["vault_path"] == f"{specs}/{DATE}-same.md"
    assert second["vault_path"] == f"{specs}/{DATE}-same-2.md"

    # A file that belongs to no review.
    stranger = tmp_path / specs / f"{DATE}-other.md"
    stranger.write_text("Jack's own notes\n")
    other = await submit(svc, title="Other")
    assert other["vault_path"] == f"{specs}/{DATE}-other-2.md"
    assert stranger.read_text() == "Jack's own notes\n"

    # A review whose file is gone still owns its path.
    (tmp_path / first["vault_path"]).unlink()
    third = await submit(svc, title="Same")
    assert third["vault_path"] == f"{specs}/{DATE}-same-3.md"


# ── 4. decide: approve ─────────────────────────────────────────────────────


async def test_approve_resolves_the_gate_and_unblocks_waiters(svc, db, hooks, tmp_path, clock):
    result = await submit(svc)
    review_id, gate_id = result["review_id"], result["gate_id"]
    await mktask(db, "impl")
    await attach(db, gate_id, "impl")
    assert (await db.get_task("impl")).is_blocked is True

    await raises("stale_revision", approve(svc, review_id, revision=2))
    await raises("stale_revision", approve(svc, review_id, revision=0))
    assert hooks.resolved == []

    clock.now = NOW + 60
    decided = await approve(svc, review_id, note="Ship it.")
    assert decided == {"review_id": review_id, "state": "approved", "unblocked_task_ids": ["impl"]}

    assert (await db.get_task("impl")).is_blocked is False
    gate = await db.get_gate(gate_id)
    assert (gate["status"], gate["resolution"], gate["resolved_by"]) == (
        "resolved",
        "approved",
        OPERATOR,
    )
    assert hooks.resolved == [(gate_id, OPERATOR, "approved")]
    assert hooks.changes == []

    review = await db.get_review(review_id)
    assert review["state"] == "approved"
    assert review["decided_by"] == OPERATOR
    assert review["decided_at"] == review["updated_at"] == NOW + 60
    assert review["decision_note"] == "Ship it."
    assert read_vault(tmp_path, result["vault_path"])[0]["status"] == "approved"
    assert read_vault(tmp_path, result["vault_path"])[1] == DOC

    [event] = hooks.of("review.decided")
    assert event["decision"] == "approve"
    assert event["decided_by"] == OPERATOR
    assert event["revision"] == 1
    assert event["unblocked_task_ids"] == ["impl"]
    assert event["note"] == "Ship it."
    assert hooks.of("spec.approved") == []
    assert [kind for kind, _ in hooks.events] == ["review.submitted", "review.decided"]


async def test_racing_decisions_let_exactly_one_win(svc, db, hooks):
    review_id = (await submit(svc))["review_id"]
    outcomes = await asyncio.gather(
        approve(svc, review_id), request_changes(svc, review_id), return_exceptions=True
    )
    wins = [o for o in outcomes if isinstance(o, dict)]
    losses = [o for o in outcomes if isinstance(o, ReviewError)]
    assert len(wins) == 1 and len(losses) == 1, outcomes
    assert losses[0].code in {"review_closed", "not_in_review", "stale_revision"}
    assert (await db.get_review(review_id))["state"] == wins[0]["state"]
    assert len(hooks.of("review.decided")) == 1


# ── 5. decide: request changes ────────────────────────────────────────────


async def test_request_changes_keeps_the_gate_open_and_hands_back_feedback(
    svc, db, hooks, tmp_path
):
    result = await submit(svc)
    review_id, gate_id = result["review_id"], result["gate_id"]
    await svc.comment(
        review_id=review_id,
        revision=1,
        quote="Body.",
        heading_path=["Doc", "Scope"],
        body="Too vague.",
        author=OPERATOR,
    )
    await svc.comment(
        review_id=review_id,
        revision=1,
        quote=None,
        heading_path=["Doc"],
        body="Missing a section.",
        author=OPERATOR,
    )

    decided = await request_changes(svc, review_id, note="Two things.")
    assert decided == {
        "review_id": review_id,
        "state": "changes_requested",
        "unblocked_task_ids": [],
    }
    review = await db.get_review(review_id)
    assert review["state"] == "changes_requested"
    assert review["decision_note"] == "Two things."
    assert (await db.get_gate(gate_id))["status"] == "open"
    assert hooks.resolved == []

    [(changed_id, feedback)] = hooks.changes
    assert changed_id == review_id
    assert feedback == ReviewService.feedback_text(review, "Two things.", 2)
    assert f"aq review show --review-id {review_id} --comments" in feedback
    assert "2 open comment(s)" in feedback
    assert "Two things." in feedback

    assert read_vault(tmp_path, result["vault_path"])[0]["status"] == "changes_requested"
    [event] = hooks.of("review.decided")
    assert event["decision"] == "request_changes"
    assert event["unblocked_task_ids"] == []


def test_feedback_text():
    review = {"id": "rev-a-b", "current_revision": 3}
    text = ReviewService.feedback_text(review, "  ", 0)
    assert text.splitlines()[0] == "Review rev-a-b (revision 3) needs changes."
    assert "(no overall note)" in text
    assert "0 open comment(s)" in text
    assert "aq review submit --review-id rev-a-b --file <draft.md>" in text


# ── 6. decide on a closed review ──────────────────────────────────────────


async def test_decide_refuses_closed_and_already_decided_reviews(svc, db):
    approved = (await submit(svc, title="A"))["review_id"]
    await approve(svc, approved)
    await raises("review_closed", approve(svc, approved))
    await raises("review_closed", request_changes(svc, approved))

    withdrawn = (await submit(svc, title="W"))["review_id"]
    await svc.withdraw(review_id=withdrawn, reason="gone", by=OPERATOR)
    await raises("review_closed", approve(svc, withdrawn))

    changed = (await submit(svc, title="C"))["review_id"]
    await request_changes(svc, changed)
    await raises("not_in_review", approve(svc, changed))
    await raises("not_in_review", request_changes(svc, changed))

    await raises("not_found", approve(svc, "rev-no-such"))


# ── 7. revise ─────────────────────────────────────────────────────────────


async def test_revise_from_in_review_and_changes_requested(svc, db, hooks, tmp_path, clock):
    result = await submit(svc)
    review_id = result["review_id"]
    comment = await svc.comment(
        review_id=review_id,
        revision=1,
        quote="Body.",
        heading_path=["Doc"],
        body="Fix",
        author=OPERATOR,
    )
    other = await svc.comment(
        review_id=review_id,
        revision=1,
        quote=None,
        heading_path=[],
        body="Also",
        author=OPERATOR,
    )

    clock.now = NOW + 10
    rev2 = "---\nignored: yes\n---\n# Doc\n\nBody, fixed.\n"
    revised = await revise(
        svc, review_id, rev2, note="Fixed body", resolves=[comment["comment_id"]]
    )
    assert revised == {
        "review_id": review_id,
        "revision": 2,
        "vault_path": result["vault_path"],
        "backup_path": None,
    }
    review = await db.get_review(review_id)
    assert (review["state"], review["current_revision"], review["updated_at"]) == (
        "in_review",
        2,
        NOW + 10,
    )
    stored = await db.get_review_revision(review_id, 2)
    assert stored["content"] == "# Doc\n\nBody, fixed.\n"
    assert stored["changes_note"] == "Fixed body"
    assert stored["submitted_task_id"] == "author"
    assert stored["submitted_at"] == NOW + 10
    comments = {c["id"]: c for c in await db.list_review_comments(review_id)}
    assert comments[comment["comment_id"]]["resolved_in_revision"] == 2
    assert comments[other["comment_id"]]["resolved_in_revision"] is None

    fm, body = read_vault(tmp_path, result["vault_path"])
    assert (fm["revision"], fm["status"], body) == (2, "in_review", "# Doc\n\nBody, fixed.\n")
    [event] = hooks.of("review.revised")
    assert event["revision"] == 2
    assert event["changes_note"] == "Fixed body"

    await request_changes(svc, review_id, revision=2)
    third = await revise(svc, review_id, "# Doc\n\nThird.\n", resolves=["no-such-comment"])
    assert third["revision"] == 3
    assert (await db.get_review(review_id))["state"] == "in_review"

    await approve(svc, review_id, revision=3)
    await raises("review_closed", revise(svc, review_id, "# Doc\n\nFourth.\n"))
    await raises("empty", revise(svc, (await submit(svc, title="E"))["review_id"], " \n"))


# ── 8. vault divergence ───────────────────────────────────────────────────


async def test_vault_divergence_blocks_decide_until_imported(svc, db, hooks, tmp_path):
    result = await submit(svc)
    review_id = result["review_id"]
    path = tmp_path / result["vault_path"]

    await raises("not_found", svc.import_edits(review_id=review_id, by=OPERATOR))

    fm, _ = split_frontmatter(path.read_text())
    edited = "# Doc\n\nJack rewrote this.\n"
    path.write_text(f"---\n{fm}\n---\n{edited}")
    shown = await svc.show(review_id=review_id)
    assert shown["vault_state"] == "diverged"
    assert svc.vault_state(await db.get_review(review_id), body_sha256(DOC)) == "diverged"
    await raises("vault_diverged", approve(svc, review_id))
    await raises("vault_diverged", request_changes(svc, review_id))
    assert path.read_text().endswith(edited)  # show never overwrites a diverged file

    imported = await svc.import_edits(review_id=review_id, by=OPERATOR)
    assert imported == {"review_id": review_id, "revision": 2}
    stored = await db.get_review_revision(review_id, 2)
    assert stored["content"] == edited
    assert stored["submitted_by"] == OPERATOR
    assert stored["submitted_task_id"] is None
    assert stored["changes_note"] == "Imported edits made directly in the vault"
    assert (await svc.show(review_id=review_id))["vault_state"] == "ok"
    assert read_vault(tmp_path, result["vault_path"])[0]["revision"] == 2
    [event] = hooks.of("review.revised")
    assert event["revision"] == 2

    await approve(svc, review_id, revision=2)
    assert (await db.get_review(review_id))["state"] == "approved"


async def test_import_edits_needs_an_open_review(svc):
    changed = (await submit(svc, title="C"))["review_id"]
    await request_changes(svc, changed)
    await raises("not_in_review", svc.import_edits(review_id=changed, by=OPERATOR))
    approved = (await submit(svc, title="A"))["review_id"]
    await approve(svc, approved)
    await raises("review_closed", svc.import_edits(review_id=approved, by=OPERATOR))


async def test_revise_backs_up_a_diverged_file_before_writing(svc, tmp_path, clock):
    result = await submit(svc)
    path = tmp_path / result["vault_path"]
    diverged = path.read_text().replace("Body.", "Jack's local edit.")
    path.write_text(diverged)

    clock.now = NOW + 5
    revised = await revise(svc, result["review_id"], "# Doc\n\nNew body.\n")
    expected = result["vault_path"].removesuffix(".md") + f".edited-{int(NOW + 5)}.md"
    assert revised["backup_path"] == expected
    assert (tmp_path / expected).read_text() == diverged
    assert read_vault(tmp_path, result["vault_path"])[1] == "# Doc\n\nNew body.\n"
    assert (await svc.show(review_id=result["review_id"]))["vault_state"] == "ok"

    # A second backup in the same second does not overwrite the first.
    path.write_text(path.read_text().replace("New body.", "Another edit."))
    again = await revise(svc, result["review_id"], "# Doc\n\nThird body.\n")
    assert again["backup_path"] not in (None, expected)
    assert (tmp_path / expected).read_text() == diverged


async def test_a_missing_vault_file_is_rewritten_by_show(svc, tmp_path):
    result = await submit(svc)
    path = tmp_path / result["vault_path"]
    path.unlink()
    assert (await svc.show(review_id=result["review_id"]))["vault_state"] == "missing"
    assert read_vault(tmp_path, result["vault_path"])[1] == DOC
    assert (await svc.show(review_id=result["review_id"]))["vault_state"] == "ok"
    # A decision on a missing file stands on the database copy and restores it.
    path.unlink()
    await approve(svc, result["review_id"])
    assert read_vault(tmp_path, result["vault_path"])[0]["status"] == "approved"


async def test_a_failed_vault_write_never_fails_the_submission(svc, db, tmp_path):
    (tmp_path / "projects").write_text("not a directory")
    result = await submit(svc)
    assert (await db.get_review(result["review_id"]))["state"] == "in_review"
    shown = await svc.show(review_id=result["review_id"])
    assert shown["vault_state"] == "missing"
    assert shown["revision"]["content"] == DOC


# ── 9. withdraw ───────────────────────────────────────────────────────────


async def test_withdraw_keeps_the_gate_and_flags_waiters(svc, db, hooks, tmp_path):
    result = await submit(svc)
    review_id, gate_id = result["review_id"], result["gate_id"]
    await mktask(db, "impl")
    await mktask(db, "impl-2")
    await attach(db, gate_id, "impl", "impl-2")

    withdrawn = await svc.withdraw(review_id=review_id, reason="superseded", by=OPERATOR)
    assert withdrawn == {"review_id": review_id, "flagged_task_ids": ["impl", "impl-2"]}

    assert (await db.get_review(review_id))["state"] == "withdrawn"
    assert (await db.get_gate(gate_id))["status"] == "open"
    assert hooks.resolved == []
    assert (await db.get_task("impl")).is_blocked is True
    for tid in ("impl", "impl-2"):
        assert await db.get_task_meta(tid, "needs_attention") == "review_withdrawn"
    flagged = hooks.of("task.needs_attention")
    assert sorted(e["task_id"] for e in flagged) == ["impl", "impl-2"]
    assert {e["reason"] for e in flagged} == {"review_withdrawn"}
    assert {e["title"] for e in flagged} == {"title impl", "title impl-2"}
    assert {e["project_id"] for e in flagged} == {PROJECT}

    [event] = hooks.of("review.withdrawn")
    assert event["reason"] == "superseded"
    assert event["flagged_task_ids"] == ["impl", "impl-2"]
    assert read_vault(tmp_path, result["vault_path"])[0]["status"] == "withdrawn"

    await raises("review_closed", svc.withdraw(review_id=review_id, reason="again", by=OPERATOR))


async def test_withdraw_from_changes_requested(svc, db):
    review_id = (await submit(svc))["review_id"]
    await request_changes(svc, review_id)
    assert (await svc.withdraw(review_id=review_id, reason="", by=OPERATOR))[
        "flagged_task_ids"
    ] == []
    assert (await db.get_review(review_id))["state"] == "withdrawn"


# ── 10. comment ───────────────────────────────────────────────────────────


async def test_comment_stores_its_anchor_and_emits(svc, db, hooks, clock):
    review_id = (await submit(svc))["review_id"]
    clock.now = NOW + 3
    made = await svc.comment(
        review_id=review_id,
        revision=1,
        quote="Intro paragraph.",
        heading_path=["Doc"],
        body="Why?",
        author=OPERATOR,
    )
    [row] = await db.list_review_comments(review_id)
    assert row == {
        "id": made["comment_id"],
        "review_id": review_id,
        "revision": 1,
        "quote": "Intro paragraph.",
        "heading_path": ["Doc"],
        "body": "Why?",
        "author": OPERATOR,
        "resolved_in_revision": None,
        "created_at": NOW + 3,
    }
    [event] = hooks.of("review.commented")
    assert event["comment_id"] == made["comment_id"]
    assert event["revision"] == 1

    section = await svc.comment(
        review_id=review_id,
        revision=1,
        quote="",
        heading_path=[],
        body="x" * 16000,
        author=OPERATOR,
    )
    assert (await db.list_review_comments(review_id))[1]["quote"] is None
    assert section["comment_id"] != made["comment_id"]


async def test_comment_validation_and_closed_reviews(svc):
    review_id = (await submit(svc))["review_id"]

    def comment(body="b", revision=1, rid=review_id):
        return svc.comment(
            review_id=rid,
            revision=revision,
            quote=None,
            heading_path=[],
            body=body,
            author=OPERATOR,
        )

    await raises("empty", comment(body=""))
    await raises("empty", comment(body=" \n "))
    await raises("too_large", comment(body="x" * 16001))
    await raises("not_found", comment(revision=2))
    await raises("not_found", comment(rid="rev-no-such"))

    await request_changes(svc, review_id)
    await comment()  # still open for comments

    await svc.withdraw(review_id=review_id, reason="", by=OPERATOR)
    await raises("review_closed", comment())
    approved = (await submit(svc, title="A"))["review_id"]
    await approve(svc, approved)
    await raises("review_closed", comment(rid=approved))


# ── 11. delegate ──────────────────────────────────────────────────────────


async def test_delegate_sets_the_decider(svc, db):
    review_id = (await submit(svc))["review_id"]
    assert await svc.delegate(review_id=review_id, to="supervisor") == {
        "review_id": review_id,
        "decider": "user_or_supervisor",
    }
    assert (await db.get_review(review_id))["decider"] == "user_or_supervisor"
    assert (await svc.delegate(review_id=review_id, to="user"))["decider"] == "user"
    assert (await db.get_review(review_id))["decider"] == "user"

    await approve(svc, review_id)
    await raises("review_closed", svc.delegate(review_id=review_id, to="supervisor"))


# ── 12. show ──────────────────────────────────────────────────────────────


async def test_show_returns_revisions_comments_and_a_block_diff(svc):
    review_id = (await submit(svc))["review_id"]
    rev2 = "# Doc\n\nIntro paragraph.\n\n## Scope\n\nBody, revised.\n\nNew section.\n"
    await revise(svc, review_id, rev2)
    await svc.comment(
        review_id=review_id,
        revision=2,
        quote="New section.",
        heading_path=["Doc", "Scope"],
        body="Good",
        author=OPERATOR,
    )

    shown = await svc.show(review_id=review_id)
    assert shown["review"]["id"] == review_id
    assert shown["revision"]["revision"] == 2
    assert shown["revision"]["content"] == rev2
    assert [r["revision"] for r in shown["revisions"]] == [1, 2]
    assert all("content" not in r for r in shown["revisions"])
    assert shown["vault_state"] == "ok"
    assert "comments" not in shown and "diff" not in shown

    first = await svc.show(review_id=review_id, revision=1, comments=True)
    assert first["revision"]["content"] == DOC
    assert first["vault_state"] == "ok"  # judged against the current revision
    assert [c["quote"] for c in first["comments"]] == ["New section."]

    diffed = await svc.show(review_id=review_id, diff_from=1)
    assert diffed["diff"] == block_diff(DOC, rev2)
    assert {"op": "added", "text": "New section."} in diffed["diff"]

    await raises("not_found", svc.show(review_id=review_id, revision=3))
    await raises("not_found", svc.show(review_id=review_id, diff_from=9))
    await raises("not_found", svc.show(review_id="rev-no-such"))
