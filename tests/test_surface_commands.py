"""Tests for the aq-surface Phase S0 CommandHandler additions.

Covers ``get_schema``, ``task_show``, ``task_set`` (``src/commands/surface_commands.py``)
and the ``task_labels`` CRUD they depend on (``src/database/queries/task_queries.py``).
See docs/specs/implementation/aq-surface.md §3, §9 (Phase S0), §10 (Test Plan).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests.db_fixtures import lease_dsn

from src.config import AppConfig, DatabaseConfig
from src.event_bus import EventBus
from src.models import Project, Task

pytestmark = pytest.mark.asyncio


async def _ensure_agent_profile(db, profile_id: str) -> None:
    """Insert a minimal ``agent_profiles`` row so a Task's ``profile_id`` FK
    resolves.  Prime does not care about profile contents — only that a
    profile-addressed message can point at a row that exists — so this
    helper stays minimal (all server_default columns unset)."""
    import time as _time

    from sqlalchemy import insert

    from src.database.tables import agent_profiles

    async with db._engine.begin() as conn:
        await conn.execute(
            insert(agent_profiles).values(
                id=profile_id,
                name=profile_id,
                created_at=_time.time(),
                updated_at=_time.time(),
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    from src.database import Database

    adapter = Database(lease_dsn("surface"))
    await adapter.initialize()
    yield adapter
    await adapter.close()


@pytest.fixture
async def handler(db):
    from src.commands.handler import CommandHandler

    config = MagicMock()
    orchestrator = MagicMock()
    orchestrator.db = db
    return CommandHandler(orchestrator=orchestrator, config=config)


@pytest.fixture
async def prime_handler(db, tmp_path):
    """A CommandHandler wired with a real AppConfig (prime needs real vault
    paths) and a real EventBus (task_handoff emits session.restart_requested).
    """
    from src.commands.handler import CommandHandler

    config = AppConfig(database=DatabaseConfig(url=lease_dsn("surface")), data_dir=str(tmp_path / "data"))
    orchestrator = MagicMock()
    orchestrator.db = db
    orchestrator.bus = EventBus()
    return CommandHandler(orchestrator=orchestrator, config=config)


@pytest.fixture
async def task(db):
    """A task belonging to a real project, ready for show/set exercises."""
    await db.create_project(Project(id="proj-1", name="Test Project"))
    t = Task(id="task-1", project_id="proj-1", title="Do the thing", description="desc")
    await db.create_task(t)
    return t


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------


class TestGetSchema:
    async def test_returns_schema_version_and_enums(self, handler):
        result = await handler.execute("get_schema", {})
        assert "error" not in result
        assert result["schema_version"] == 1
        assert set(result["enums"].keys()) == {
            "task_status",
            "task_type",
            "dependency_type",
            "gate_type",
            "gate_status",
            "hierarchy_error",
            "claim_result",
            "claim_phase",
            "lifecycle",
            "session_state",
            "agent_state",
            "outcome",
        }

    async def test_task_status_enum_matches_models(self, handler):
        from src.models import TaskStatus

        result = await handler.execute("get_schema", {})
        assert result["enums"]["task_status"] == [s.value for s in TaskStatus]

    async def test_dependency_type_enum_matches_tables(self, handler):
        from src.database.tables import TASK_DEP_TYPES

        result = await handler.execute("get_schema", {})
        assert result["enums"]["dependency_type"] == list(TASK_DEP_TYPES)

    async def test_gate_type_and_status_match_tables(self, handler):
        from src.database.tables import GATE_STATUSES, GATE_TYPES

        result = await handler.execute("get_schema", {})
        assert result["enums"]["gate_type"] == list(GATE_TYPES)
        assert result["enums"]["gate_status"] == list(GATE_STATUSES)


# ---------------------------------------------------------------------------
# task_show
# ---------------------------------------------------------------------------


class TestTaskShow:
    async def test_preparation_input_is_readable_only_on_own_task(self, handler, db, task):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
        from src.profiles.capabilities import CapabilityPolicy
        from src.api.auth import RequestScope
        from src.api.scope import check_command_scope

        notes_input = {"source_digest": "digest", "sources": [{"task": "feature"}]}
        await db.set_task_meta(task.id, "notes_input", notes_input)
        other = Task(id="other", project_id=task.project_id, title="Other", description="")
        await db.create_task(other)
        policy = CapabilityPolicy.from_namespaces(aq_commands=["task_show", "task_set"],
                                                   harness_tools=[], plugin_tools=[])
        principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=policy,
                                       session_id="worker", task_id=task.id,
                                       project_id=task.project_id)
        with principal_context(principal):
            scope = {"kind": "session", "task_id": task.id, "project_id": task.project_id,
                     "session_id": "worker"}
            shown = await handler.execute("task_show", {"task_id": task.id, "_scope": scope})
            assert shown["metadata"]["notes_input"] == notes_input
            refused = check_command_scope("task_show", {"task_id": other.id}, RequestScope(**scope))
            assert refused == "out of scope: task_id mismatch"
            changed = await handler.execute("task_set", {"task_id": task.id,
                                                         "meta": {"notes_input": {}}, "_scope": scope})
            assert "immutable" in changed["error"]
        assert await db.get_task_meta(task.id, "notes_input") == notes_input

    async def test_missing_task_id(self, handler):
        result = await handler.execute("task_show", {})
        assert "error" in result

    async def test_unknown_task(self, handler):
        result = await handler.execute("task_show", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_composes_fields_context_and_labels(self, handler, db, task):
        await db.add_task_context(task.id, type="note", label="note", content="hello from context")
        await db.add_task_label(task.id, "urgent")

        result = await handler.execute("task_show", {"task_id": task.id})
        assert "error" not in result
        assert result["id"] == "task-1"
        assert result["title"] == "Do the thing"
        assert result["labels"] == ["urgent"]
        assert len(result["context"]) == 1
        assert result["context"][0]["content"] == "hello from context"

    async def test_no_labels_or_context_is_empty_not_missing(self, handler, task):
        result = await handler.execute("task_show", {"task_id": task.id})
        assert result["labels"] == []
        assert result["context"] == []

    async def test_exposes_resumed_parent_delivery_projection(
        self, handler, db, task, monkeypatch
    ):
        from unittest.mock import AsyncMock
        from sqlalchemy import update
        from src.database.tables import projects

        async with db.immediate() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == task.project_id)
                .values(hierarchical_integration_mode="hierarchy")
            )
        monkeypatch.setattr(
            db,
            "get_integration_checkpoint",
            AsyncMock(
                return_value={
                    "task_id": task.id,
                    "generation": 3,
                    "checkpoint_sha": "a" * 40,
                    "episode_id": None,
                    "state": "working",
                }
            ),
        )

        result = await handler.execute("task_show", {"task_id": task.id})

        assert result["integration_delivery"] == {
            "outcome": "working",
            "task_id": task.id,
            "generation": 3,
            "checkpoint_sha": "a" * 40,
            "episode_id": None,
            "operation_id": None,
            "head_sha": "a" * 40,
            "receipts": [],
            "blockers": [],
        }

    async def _root_delivery_fixture(self, db, task, monkeypatch, *, receipts, reviews):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from sqlalchemy import update
        from src.database.tables import projects

        async with db.immediate() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == task.project_id)
                .values(hierarchical_integration_mode="train")
            )
        monkeypatch.setattr(
            db,
            "get_integration_checkpoint",
            AsyncMock(
                return_value={
                    "task_id": task.id,
                    "repository_id": "repo-1",
                    "generation": 2,
                    "checkpoint_sha": "a" * 40,
                    "episode_id": None,
                    "state": "working",
                }
            ),
        )
        monkeypatch.setattr(
            db, "get_repo", AsyncMock(return_value=SimpleNamespace(default_branch="main"))
        )
        listing = AsyncMock(return_value=receipts)
        monkeypatch.setattr(db, "list_integration_delivery_receipts", listing)
        monkeypatch.setattr(
            db,
            "get_integration_review_evidence",
            AsyncMock(side_effect=lambda evidence_id: reviews.get(evidence_id)),
        )
        return listing

    @staticmethod
    def _root_receipt(receipt_id, head, evidence_id, disposition="code", batch_id="b1"):
        return {
            "id": receipt_id,
            "source_task_id": "task-1",
            "reviewed_head_sha": head,
            "disposition": disposition,
            "batch_id": batch_id,
            "review_evidence": {"review_evidence_id": evidence_id},
        }

    async def test_root_task_shows_exact_root_batch_receipt(
        self, handler, db, task, monkeypatch
    ):
        exact = self._root_receipt("r-exact", "a" * 40, "ev-current")
        listing = await self._root_delivery_fixture(
            db,
            task,
            monkeypatch,
            receipts=[exact],
            reviews={
                "ev-current": {
                    "source_task_id": task.id,
                    "reviewed_head_sha": "a" * 40,
                    "generation": 2,
                }
            },
        )

        result = await handler.execute("task_show", {"task_id": task.id})

        delivery = result["integration_delivery"]
        assert delivery["outcome"] == "delivered"
        assert delivery["receipts"] == [exact]
        listing.assert_awaited_once_with(
            source_task_id=task.id, repository_id="repo-1", target_branch="refs/heads/main"
        )

    async def test_root_task_ignores_changed_head_and_old_generation_receipts(
        self, handler, db, task, monkeypatch
    ):
        await self._root_delivery_fixture(
            db,
            task,
            monkeypatch,
            receipts=[
                self._root_receipt("r-old-head", "b" * 40, "ev-old-head"),
                self._root_receipt("r-old-gen", "a" * 40, "ev-old-gen"),
                self._root_receipt("r-noop", "a" * 40, "ev-current", disposition="noop"),
                self._root_receipt("r-parent", "a" * 40, "ev-current", batch_id=None),
            ],
            reviews={
                "ev-old-head": {
                    "source_task_id": task.id,
                    "reviewed_head_sha": "b" * 40,
                    "generation": 2,
                },
                "ev-old-gen": {
                    "source_task_id": task.id,
                    "reviewed_head_sha": "a" * 40,
                    "generation": 1,
                },
                "ev-current": {
                    "source_task_id": task.id,
                    "reviewed_head_sha": "a" * 40,
                    "generation": 2,
                },
            },
        )

        result = await handler.execute("task_show", {"task_id": task.id})

        assert result["integration_delivery"]["outcome"] == "working"
        assert result["integration_delivery"]["receipts"] == []

    async def test_review_evidence_lookup_by_id_misses_cleanly(self, db):
        assert await db.get_integration_review_evidence("missing") is None

    async def test_includes_latest_completion_story(self, handler, db, task):
        """A re-close must expose the newest durable completion account."""
        from src.models import TaskCompletion

        await db.save_task_completion(
            TaskCompletion(
                id="completion-old",
                task_id=task.id,
                outcome="fail",
                summary="First close failed.",
                completed_at=1000.0,
            )
        )
        await db.save_task_completion(
            TaskCompletion(
                id="completion-new",
                task_id=task.id,
                outcome="pass",
                work_outcome="shipped",
                changes="Added completion records.",
                verification="Backend and dashboard tests passed.",
                tests=["pytest tests/test_surface_commands.py -q"],
                commands=["ruff check src tests"],
                branch="feature/completion",
                commits=["abc123"],
                pr_url="https://github.com/example/repo/pull/17",
                summary="Completion details now survive close.",
                notes="Ready for review.",
                completed_at=1234.5,
            )
        )

        result = await handler.execute("task_show", {"task_id": task.id})

        assert result["completion"] == {
            "id": "completion-new",
            "task_id": task.id,
            "outcome": "pass",
            "work_outcome": "shipped",
            "failure_class": None,
            "changes": "Added completion records.",
            "verification": "Backend and dashboard tests passed.",
            "tests": ["pytest tests/test_surface_commands.py -q"],
            "commands": ["ruff check src tests"],
            "branch": "feature/completion",
            "commits": ["abc123"],
            "pr_url": "https://github.com/example/repo/pull/17",
            "summary": "Completion details now survive close.",
            "notes": "Ready for review.",
            "deliverables": [],
            "completed_at": 1234.5,
        }


# ---------------------------------------------------------------------------
# task_set
# ---------------------------------------------------------------------------


class TestTaskSet:
    async def test_missing_task_id(self, handler):
        result = await handler.execute("task_set", {})
        assert "error" in result

    async def test_unknown_task(self, handler):
        result = await handler.execute("task_set", {"task_id": "nope", "note": "x"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_no_fields_is_an_error(self, handler, task):
        result = await handler.execute("task_set", {"task_id": task.id})
        assert "error" in result

    async def test_branch_and_pr_url(self, handler, db, task):
        result = await handler.execute(
            "task_set",
            {"task_id": task.id, "branch": "feat/x", "pr_url": "https://example/pr/1"},
        )
        assert "error" not in result
        assert set(result["fields_changed"]) == {"branch_name", "pr_url"}

        updated = await db.get_task(task.id)
        assert updated.branch_name == "feat/x"
        assert updated.pr_url == "https://example/pr/1"

    async def test_checkpointed_branch_refuses_rename_before_other_writes(
        self, handler, db, task
    ):
        import time

        from sqlalchemy import insert

        from src.database.tables import task_integration_checkpoints

        async with db.immediate() as conn:
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id=task.id, repository_id="repo", branch="aq/task-1",
                updated_at=time.time(),
            ))

        refused = await handler.execute("task_set", {
            "task_id": task.id, "branch": "aq/epic/renamed",
            "description": "unexpected", "pr_url": "https://example/pr/2",
        })
        assert "canonical integration branch" in refused["error"]
        unchanged = await db.get_task(task.id)
        assert unchanged.branch_name is None
        assert unchanged.description == "desc"
        assert unchanged.pr_url is None

        with pytest.raises(ValueError, match="canonical integration branch"):
            await db.update_task(task.id, branch_name="aq/epic/renamed")

        restored = await handler.execute("task_set", {
            "task_id": task.id, "branch": "aq/task-1",
        })
        assert "error" not in restored
        assert (await db.get_task(task.id)).branch_name == "aq/task-1"

    async def test_never_touches_status(self, handler, db, task):
        before = await db.get_task(task.id)
        await handler.execute("task_set", {"task_id": task.id, "note": "progress update"})
        after = await db.get_task(task.id)
        assert after.status == before.status

    async def test_note_becomes_task_context(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "note": "progress update"})
        contexts = await db.get_task_contexts(task.id)
        assert any(c["content"] == "progress update" for c in contexts)

    async def test_labels_add_and_remove(self, handler, db, task):
        result = await handler.execute("task_set", {"task_id": task.id, "labels_add": ["a", "b"]})
        assert set(await db.get_task_labels(task.id)) == {"a", "b"}
        assert "+label:a" in result["fields_changed"]
        assert "+label:b" in result["fields_changed"]

        result2 = await handler.execute("task_set", {"task_id": task.id, "labels_remove": ["a"]})
        assert await db.get_task_labels(task.id) == ["b"]
        assert "-label:a" in result2["fields_changed"]

    async def test_meta_round_trips_through_get_all_task_meta(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "meta": {"foo": "bar", "count": 3}})
        meta = await db.get_all_task_meta(task.id)
        assert meta == {"foo": "bar", "count": 3}

    async def test_work_dir_stored_as_metadata(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "work_dir": "/tmp/work"})
        assert await db.get_task_meta(task.id, "work_dir") == "/tmp/work"

    async def test_returns_task_show_shape_with_fields_changed(self, handler, task):
        result = await handler.execute("task_set", {"task_id": task.id, "note": "x"})
        # Same composition as task_show (fields + context + labels) plus the delta.
        assert result["id"] == task.id
        assert "context" in result
        assert "labels" in result
        assert "fields_changed" in result


# ---------------------------------------------------------------------------
# task_labels DB layer (src/database/queries/task_queries.py)
# ---------------------------------------------------------------------------


class TestTaskLabelsDbLayer:
    async def test_add_is_idempotent(self, db, task):
        await db.add_task_label(task.id, "dup")
        await db.add_task_label(task.id, "dup")
        assert await db.get_task_labels(task.id) == ["dup"]

    async def test_remove_missing_is_a_noop(self, db, task):
        await db.remove_task_label(task.id, "never-added")
        assert await db.get_task_labels(task.id) == []

    async def test_labels_sorted(self, db, task):
        await db.add_task_label(task.id, "zeta")
        await db.add_task_label(task.id, "alpha")
        assert await db.get_task_labels(task.id) == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# prime — Phase S1 (docs/specs/implementation/aq-surface.md §3)
# ---------------------------------------------------------------------------


class TestPrime:
    async def test_missing_task_id_is_an_error(self, prime_handler):
        result = await prime_handler.execute("prime", {})
        assert "error" in result
        assert "task_id" in result["error"]

    async def test_unknown_task_is_an_error(self, prime_handler):
        result = await prime_handler.execute("prime", {"task_id": "nope"})
        assert "error" in result

    async def test_success_shape(self, prime_handler, db, task):
        result = await prime_handler.execute("prime", {"task_id": task.id})
        assert result["success"] is True
        assert isinstance(result["body"], str)
        assert isinstance(result["sections"], list)
        assert {s["key"] for s in result["sections"]} >= {"task", "tool_guidance"}
        assert result["source"] == "default"
        assert isinstance(result["tokens_est"], int)
        assert task.id in result["body"]

    async def test_session_id_and_work_dir_are_forwarded(self, prime_handler, db, task):
        result = await prime_handler.execute(
            "prime", {"task_id": task.id, "session_id": "sess-1", "work_dir": "/work/x"}
        )
        assert result["success"] is True
        workspaces = next(s for s in result["sections"] if s["key"] == "workspaces")
        assert "/work/x" in workspaces["body"]

    async def test_repair_context_uses_authenticated_session(
        self, prime_handler, db, task, monkeypatch
    ):
        from unittest.mock import AsyncMock
        from sqlalchemy import update
        from src.database.tables import tasks

        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task.id).values(
                created_by_kind="integration_repair",
            ))
        read = AsyncMock(return_value={"intent_id": "current-conflict", "fence": {"token": 9}})
        monkeypatch.setattr(db, "get_parent_repair_prime_context", read)
        prime_handler._current_scope = {
            "kind": "session", "task_id": task.id, "project_id": task.project_id,
            "session_id": "authenticated-session",
        }
        result = await prime_handler._cmd_prime({
            "task_id": task.id, "session_id": "caller-selected-session",
        })
        assert result["success"] is True
        read.assert_awaited_once_with(task.id, session_id="authenticated-session")
        assert '"intent_id": "current-conflict"' in result["body"]

    async def test_not_gated_by_memory_pause(self, prime_handler, db, task):
        # `prime` itself is not a memory command — the paused gate must not
        # touch it even though its L1/L2 slots render empty.
        result = await prime_handler.execute("prime", {"task_id": task.id})
        assert "error" not in result or result.get("success") is True

    async def test_pending_message_rendered_and_marked_delivered(
        self, prime_handler, db, task
    ):
        """Task 3: prime is a delivery method, not a peek.

        A pending ``to_kind="task"`` message appears in the ``messages``
        section (with the same envelope as the inject hook) and is marked
        delivered via CAS with ``via="prime"`` after rendering.  Gated on
        ``config.messages.enabled``.
        """
        prime_handler.config.messages.enabled = True
        msg = await db.create_message(
            project_id=task.project_id,
            from_kind="user",
            from_id="discord:1",
            to_kind="task",
            to_id=task.id,
            body="a note for the task",
            subject="hi",
        )
        assert msg.delivered_at is None

        result = await prime_handler.execute("prime", {"task_id": task.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        # Envelope shape matches _render_nudge / aq inbox --inject.
        assert msg.id in messages["body"]
        assert "user:discord:1" in messages["body"]
        assert "a note for the task" in messages["body"]

        stored = await db.get_message(msg.id)
        assert stored.delivered_at is not None
        assert stored.via == "prime"

    async def test_pending_message_skipped_when_messages_disabled(
        self, prime_handler, db, task
    ):
        """messages.enabled=False → no render, no mark_delivered."""
        prime_handler.config.messages.enabled = False
        msg = await db.create_message(
            project_id=task.project_id,
            from_kind="user",
            from_id="discord:1",
            to_kind="task",
            to_id=task.id,
            body="not for you",
        )
        result = await prime_handler.execute("prime", {"task_id": task.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        assert msg.id not in messages["body"]
        stored = await db.get_message(msg.id)
        assert stored.delivered_at is None

    async def test_profile_addressed_message_rendered_and_marked_delivered(
        self, prime_handler, db
    ):
        """Fix: prime must surface ``to_kind='profile'`` messages too.

        The prime spec's message-gathering path was fetching only
        ``("task", task_id)``; profile-addressed rows never made it to
        the priming agent.  This test pins the fix — a message addressed
        to the task's profile appears in the messages section and is
        marked delivered via CAS with ``via="prime"``.
        """
        await db.create_project(Project(id="pp", name="Profile Project"))
        await _ensure_agent_profile(db, "claude-agent")
        t = Task(
            id="task-with-profile",
            project_id="pp",
            title="T",
            description="d",
            profile_id="claude-agent", route_source="legacy",
        )
        await db.create_task(t)

        prime_handler.config.messages.enabled = True
        msg = await db.create_message(
            project_id=t.project_id,
            from_kind="user",
            from_id="u:1",
            to_kind="profile",
            to_id="claude-agent",
            body="hello profile",
        )

        result = await prime_handler.execute("prime", {"task_id": t.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        assert msg.id in messages["body"]
        assert "hello profile" in messages["body"]

        stored = await db.get_message(msg.id)
        assert stored.delivered_at is not None
        assert stored.via == "prime"

    async def test_session_addressed_message_rendered_when_session_resolvable(
        self, prime_handler, db
    ):
        """Session-addressed message surfaces once a session row exists.

        The priming session's name is the ``s-…`` form on the
        ``sessions`` table; ``get_session_for_task`` is the resolver.
        When resolvable, ``("session", <name>)`` gets fetched alongside
        task/profile inboxes.
        """
        import time as _time

        from src.models import SessionRecord

        await db.create_project(Project(id="ps", name="Session Project"))
        await _ensure_agent_profile(db, "claude-agent")
        t = Task(
            id="task-with-session",
            project_id="ps",
            title="T",
            description="d",
            profile_id="claude-agent", route_source="legacy",
        )
        await db.create_task(t)

        sess_name = f"s-{t.id}"
        await db.create_session(
            SessionRecord(
                id="sess-abc",
                project_id="ps",
                profile_id="claude-agent",
                harness="claude",
                provider="fake",
                name=sess_name,
                lifecycle="task",
                work_dir="/tmp/x",
                epoch="e",
                instance_token="tok",
                started_at=_time.time(),
                task_id=t.id,
                state="running",
            )
        )

        prime_handler.config.messages.enabled = True
        msg = await db.create_message(
            project_id=t.project_id,
            from_kind="user",
            from_id="u:1",
            to_kind="session",
            to_id=sess_name,
            body="hello session",
        )

        result = await prime_handler.execute("prime", {"task_id": t.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        assert msg.id in messages["body"]
        assert "hello session" in messages["body"]

        stored = await db.get_message(msg.id)
        assert stored.delivered_at is not None
        assert stored.via == "prime"

    async def test_task_profile_session_merged_by_priority_no_dupes(
        self, prime_handler, db
    ):
        """Merging three inboxes: dedupe by id, sort by (priority, created_at).

        Three messages are inserted with distinct priorities and mixed
        recipient kinds; a fourth is a duplicate that the merged path
        would double-render if de-duping were absent.
        """
        import time as _time

        from src.models import SessionRecord

        await db.create_project(Project(id="pm", name="Merge Project"))
        await _ensure_agent_profile(db, "claude-agent")
        t = Task(
            id="task-merge",
            project_id="pm",
            title="T",
            description="d",
            profile_id="claude-agent", route_source="legacy",
        )
        await db.create_task(t)

        sess_name = f"s-{t.id}"
        await db.create_session(
            SessionRecord(
                id="sess-m",
                project_id="pm",
                profile_id="claude-agent",
                harness="claude",
                provider="fake",
                name=sess_name,
                lifecycle="task",
                work_dir="/tmp/x",
                epoch="e",
                instance_token="tok",
                started_at=_time.time(),
                task_id=t.id,
                state="running",
            )
        )

        prime_handler.config.messages.enabled = True
        m_task = await db.create_message(
            project_id="pm",
            from_kind="user", from_id="u:1",
            to_kind="task", to_id=t.id,
            body="task-body", priority=50,
        )
        m_profile = await db.create_message(
            project_id="pm",
            from_kind="user", from_id="u:1",
            to_kind="profile", to_id="claude-agent",
            body="profile-body", priority=10,
        )
        m_session = await db.create_message(
            project_id="pm",
            from_kind="user", from_id="u:1",
            to_kind="session", to_id=sess_name,
            body="session-body", priority=30,
        )

        result = await prime_handler.execute("prime", {"task_id": t.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        body = messages["body"]

        # All three appear exactly once (dedupe check — each id occurs
        # only once even though inboxes are queried separately).
        for mid in (m_task.id, m_profile.id, m_session.id):
            assert body.count(mid) == 1

        # Priority order: profile (10) < session (30) < task (50).
        pos_profile = body.index(m_profile.id)
        pos_session = body.index(m_session.id)
        pos_task = body.index(m_task.id)
        assert pos_profile < pos_session < pos_task

        # All three marked delivered via prime.
        for mid in (m_task.id, m_profile.id, m_session.id):
            stored = await db.get_message(mid)
            assert stored.delivered_at is not None
            assert stored.via == "prime"


# ---------------------------------------------------------------------------
# task_handoff — Phase S1 (design §6.1)
# ---------------------------------------------------------------------------


class TestTaskHandoff:
    async def test_missing_task_id_is_an_error(self, prime_handler):
        result = await prime_handler.execute("task_handoff", {})
        assert "error" in result

    async def test_unknown_task_is_an_error(self, prime_handler):
        result = await prime_handler.execute("task_handoff", {"task_id": "nope"})
        assert "error" in result

    async def test_writes_handoff_task_context_row(self, prime_handler, db, task):
        result = await prime_handler.execute(
            "task_handoff",
            {"task_id": task.id, "subject": "partial fix", "detail": "stopped early"},
        )
        assert result["success"] is True
        assert result["handoff_id"]

        contexts = await db.get_task_contexts(task.id)
        handoff_rows = [c for c in contexts if c["type"] == "handoff"]
        assert len(handoff_rows) == 1
        payload = json.loads(handoff_rows[0]["content"])
        assert payload["subject"] == "partial fix"
        assert payload["detail"] == "stopped early"

    async def test_non_auto_requests_restart_and_emits_event(self, prime_handler, db, task):
        received = []
        prime_handler.orchestrator.bus.subscribe(
            "session.restart_requested", lambda data: received.append(data)
        )

        result = await prime_handler.execute("task_handoff", {"task_id": task.id})
        assert result["restart_requested"] is True
        assert len(received) == 1
        assert received[0]["task_id"] == task.id
        assert received[0]["reason"] == "handoff"

    async def test_auto_never_requests_restart_or_emits_event(self, prime_handler, db, task):
        received = []
        prime_handler.orchestrator.bus.subscribe(
            "session.restart_requested", lambda data: received.append(data)
        )

        result = await prime_handler.execute("task_handoff", {"task_id": task.id, "auto": True})
        assert result["restart_requested"] is False
        assert received == []

    async def test_handoff_note_appears_in_next_prime_render(self, prime_handler, db, task):
        await prime_handler.execute(
            "task_handoff", {"task_id": task.id, "auto": True, "subject": "s", "detail": "d"}
        )
        result = await prime_handler.execute("prime", {"task_id": task.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        assert "s" in messages["body"]
        assert "d" in messages["body"]


class TestStructuredHandoff:
    async def test_empty_auto_preserves_useful_note(self, prime_handler, db, task):
        saved = await prime_handler.execute(
            "task_handoff",
            {
                "task_id": task.id,
                "auto": True,
                "goal": "Fix compaction",
                "next_step": "Run the renderer tests",
                "uncertainties": ["Check quoting"],
            },
        )
        for _ in range(3):
            result = await prime_handler.execute(
                "task_handoff",
                {
                    "task_id": task.id,
                    "auto": True,
                    "detail": "  ",
                    "completed": [""],
                },
            )
            assert result["created"] and not result["restart_requested"]
        rows = await db.get_task_contexts(task.id)
        assert len(rows) == 4
        snapshots = [json.loads(row["content"]) for row in rows
                     if row["id"] != saved["handoff_id"]]
        assert all(note["facts_only"] and note["facts"]["task_id"] == task.id
                   for note in snapshots)
        body = (await prime_handler.execute("prime", {"task_id": task.id}))["body"]
        assert "Run the renderer tests" in body
        assert "Check quoting" in body
        assert "Current daemon facts" in body

    async def test_keyed_retries_are_atomic_and_emit_restart_once(self, prime_handler, db, task):
        import asyncio

        received = []
        prime_handler.orchestrator.bus.subscribe(
            "session.restart_requested", lambda data: received.append(data)
        )
        args = {"task_id": task.id, "next_step": "Continue", "idempotency_key": "hook-1"}
        results = await asyncio.gather(
            *(prime_handler.execute("task_handoff", args) for _ in range(4))
        )
        assert all(r.get("success") for r in results), results
        assert len({r["handoff_id"] for r in results}) == 1
        assert sum(r["created"] for r in results) == 1
        assert len(received) == 1
        assert len(await db.get_task_contexts(task.id)) == 1
        await db.update_task(task.id, claim_epoch=1)
        result = await prime_handler.execute("task_handoff", args)
        assert result["created"] and result["handoff_id"] != results[0]["handoff_id"]

    @pytest.mark.parametrize(
        "fields",
        [
            {"next_step": "😀" * 2049},
            {"subject": "a" * 4096, "detail": "b" * 4097},
            {"uncertainties": ["u"] * 21},
            {"completed": ["c"] * 21},
            {"schema_version": 2},
            {"facts": {"head": "forged"}},
            {"claim_epoch": "invalid"},
        ],
    )
    async def test_invalid_note_does_not_write(self, prime_handler, db, task, fields):
        result = await prime_handler.execute("task_handoff", {"task_id": task.id, **fields})
        assert "error" in result
        assert await db.get_task_contexts(task.id) == []

    async def test_annotation_uses_real_checkout_and_current_claim(
        self, prime_handler, db, task, tmp_path
    ):
        import subprocess

        for cmd in (
            ["git", "init", "-b", "actual"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "commit", "--allow-empty", "-m", "base"],
        ):
            subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
        for i in range(25):
            (tmp_path / f"dirty {i}.txt").write_text("dirty")
        await db.set_task_meta(task.id, "work_dir", str(tmp_path))
        await db.update_task(task.id, branch_name="wrong-task-branch", claim_epoch=7)
        result = await prime_handler.execute(
            "task_handoff",
            {
                "task_id": task.id,
                "files": ["asserted.py"],
                "next_step": "Continue",
                "auto": True,
            },
        )
        assert result.get("success"), result
        row = (await db.get_task_contexts(task.id))[0]
        payload = json.loads(row["content"])
        assert payload["schema_version"] == 1
        assert payload["agent"]["files"] == ["asserted.py"]
        assert payload["facts"]["branch"] == "actual"
        assert len(payload["facts"]["head"]) == 40
        assert payload["facts"]["claim_epoch"] == row["claim_epoch"] == 7
        assert payload["facts"]["dirty_path_count"] == 25
        assert len(payload["facts"]["dirty_paths"]) == 20
        assert payload["created_at"] == row["created_at"]
        assert payload["facts"]["subtasks"] == {"total": 0, "settled": 0}
        await db.update_task(task.id, claim_epoch=8)
        body = (await prime_handler.execute("prime", {"task_id": task.id}))["body"]
        assert "Stale handoff files/checkout" in body
        assert "claim_epoch: 8" in body
        assert "actual" in body

    async def test_write_rechecks_epoch_after_fact_capture(
        self, prime_handler, db, task, monkeypatch
    ):
        from src import handoffs

        original = handoffs.collect_facts

        async def race(*args, **kwargs):
            facts = await original(*args, **kwargs)
            await db.update_task(task.id, claim_epoch=1)
            return facts

        monkeypatch.setattr(handoffs, "collect_facts", race)
        result = await prime_handler.execute(
            "task_handoff", {"task_id": task.id, "goal": "Continue"}
        )
        assert "stale_claim" in result["error"]
        assert await db.get_task_contexts(task.id) == []
