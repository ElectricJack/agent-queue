"""Tests for close-with-summary enforcement (Dv2 Phase 2 Task 2).

Verifies:
- Tasks whose profile has ``needs_workspace=True`` must supply a ``summary``
  on close; omitting it returns a structured error.
- Tasks with ``needs_workspace=False`` (or no profile) may omit summary.
- When no explicit ``commit`` is passed but the task has a ``branch_name``,
  the SHA is captured via ``GitManager.arev_parse`` into ``work_commit_auto``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.assignment_routing_helpers import route_source_for


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _StubBus:
    """Records what the close path publishes, so a test can assert on it."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload):
        self.events.append((event_type, payload))


class _StubOrchestrator:
    """Minimal orchestrator stub sufficient for _cmd_task_close."""

    def __init__(self, db):
        self.db = db
        self.plugin_registry = None
        self.token_store = None
        self.git = MagicMock()
        self.bus = _StubBus()

    async def complete_session_task(self, task, **kwargs):
        status = TaskStatus.COMPLETED if kwargs.get("outcome") == "pass" else TaskStatus.FAILED
        await self.db.transition_task(
            task.id,
            status,
            context="session_close",
            skip_open_subtasks=kwargs.get("skip_open_subtasks", False),
        )
        return {"status": status.value, "pr_url": None, "pipeline_ok": True}


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("t2.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.messages.enabled = False
    return cfg


@pytest.fixture
def handler(db, config):
    orch = _StubOrchestrator(db)
    return CommandHandler(orch, config)


async def _create_routed(handler, db, profile_id: str) -> str:
    """File a task with hints only, then route it as the router would.

    A filing may not name a profile (mandatory task routing §5.1); the tests
    below only need a task running on *profile_id*.
    """
    created = await handler.execute("create_task", {"project_id": "p", "title": "t"})
    tid = created["created"]
    assert await db.update_task_routing(
        tid, profile_id=profile_id, route_source=route_source_for(profile_id), intelligence_class=None, preferred_workspace_id=None
    )
    return tid


# ---------------------------------------------------------------------------
# Test 1: summary required for workspace-needing profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_rejects_missing_summary_for_workspace_profile(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result.get("success") is not True
    assert "summary" in result["error"].lower()
    assert await db.get_task_completion(tid) is None


# ---------------------------------------------------------------------------
# Test 2: summary not required for non-workspace profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_allows_missing_summary_for_non_workspace_profile(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="chat", name="Chat", needs_workspace=False))
    tid = await _create_routed(handler, db, "chat")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 3: summary not required for tasks with no profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_allows_missing_summary_for_profileless_task(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})

    assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 4: summary succeeds and is written to task_meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_summary_succeeds_and_stores_meta(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "did the thing"},
    )

    assert result["success"] is True
    assert await db.get_task_meta(tid, "summary") == "did the thing"


# ---------------------------------------------------------------------------
# Test 5: commit hash captured from branch_name via arev_parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_captures_commit_from_branch(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.update_task(tid, branch_name="feature/x")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    fake_sha = "deadbeef" * 5  # 40 chars

    async def fake_arev_parse(checkout_path, ref):
        assert ref == "feature/x"
        return fake_sha

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    # We also need get_project_workspace_path to return a non-None path.
    # The project was created without a workspace row, so we create one here.
    from src.models import RepoSourceType, Workspace

    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p",
            workspace_path="/tmp/p",
            source_type=RepoSourceType.LINK,
        )
    )

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "did the thing"},
    )
    assert result["success"] is True
    meta = await db.get_task_meta(tid, "work_commit_auto")
    assert meta == fake_sha


# ---------------------------------------------------------------------------
# Test 6: explicit commit skips arev_parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_skips_auto_commit_when_explicit_commit_provided(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.update_task(tid, branch_name="feature/y")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    arev_called = []

    async def fake_arev_parse(checkout_path, ref):
        arev_called.append(ref)
        return "a" * 40

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "summary": "done",
            "commit": "explicit-sha",
        },
    )
    assert result["success"] is True
    assert arev_called == [], "arev_parse must NOT be called when caller supplies commit"
    assert await db.get_task_meta(tid, "work_commit") == "explicit-sha"
    assert await db.get_task_meta(tid, "work_commit_auto") is None


# ---------------------------------------------------------------------------
# Test 7: arev_parse failure is non-fatal (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_succeeds_even_when_arev_parse_returns_none(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.update_task(tid, branch_name="feature/z")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    from src.models import RepoSourceType, Workspace

    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p",
            workspace_path="/tmp/p",
            source_type=RepoSourceType.LINK,
        )
    )

    async def fake_arev_parse(checkout_path, ref):
        return None  # simulate git failure / unknown ref

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "completed"},
    )
    assert result["success"] is True
    assert await db.get_task_meta(tid, "work_commit_auto") is None


@pytest.mark.asyncio
async def test_close_captures_auto_commit_sha_after_completion_pipeline(handler, db, monkeypatch):
    """Auto-remediation in the pipeline must be reflected in the durable commit."""
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.update_task(tid, branch_name="feature/auto-remediated")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    from src.models import RepoSourceType, Workspace

    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p",
            workspace_path="/tmp/p",
            source_type=RepoSourceType.LINK,
        )
    )
    pipeline_finished = False
    original_complete = handler.orchestrator.complete_session_task

    async def complete_with_auto_commit(*args, **kwargs):
        nonlocal pipeline_finished
        result = await original_complete(*args, **kwargs)
        pipeline_finished = True
        return result

    async def final_sha(checkout_path, ref):
        return "after-pipeline" if pipeline_finished else "before-pipeline"

    monkeypatch.setattr(handler.orchestrator, "complete_session_task", complete_with_auto_commit)
    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", final_sha)

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "auto-remediated"},
    )

    assert result["success"] is True
    assert await db.get_task_meta(tid, "work_commit_auto") == "after-pipeline"
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.commits == ["after-pipeline"]


@pytest.mark.asyncio
async def test_close_persists_structured_completion_story(handler, db):
    """A successful close must save the normalized completion account."""
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    tid = await _create_routed(handler, db, "worker")
    await db.update_task(
        tid,
        branch_name="feature/completion-story",
        pr_url="https://github.com/example/repo/pull/17",
    )
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "work_outcome": "shipped",
            "summary": "Added durable completion records.",
            "changes": "Added the model, persistence, and task detail surfaces.",
            "verification": "Focused backend and dashboard tests passed.",
            "tests": ["pytest tests/test_task_close_summary_enforcement.py -q"],
            "commands": ["npm test -- task-detail", "ruff check src tests"],
            "commit": "abc123",
            "notes": "Ready for reviewer.",
        },
    )

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.task_id == tid
    assert completion.outcome == "pass"
    assert completion.work_outcome == "shipped"
    assert completion.changes == "Added the model, persistence, and task detail surfaces."
    assert completion.verification == "Focused backend and dashboard tests passed."
    assert completion.tests == ["pytest tests/test_task_close_summary_enforcement.py -q"]
    assert completion.commands == ["npm test -- task-detail", "ruff check src tests"]
    assert completion.branch == "feature/completion-story"
    assert completion.commits == ["abc123"]
    assert completion.pr_url == "https://github.com/example/repo/pull/17"
    assert completion.summary == "Added durable completion records."
    assert completion.notes == "Ready for reviewer."


@pytest.mark.asyncio
async def test_close_accepts_declared_file_deliverables_and_records_their_evaluation(handler, db):
    """A passing close preserves positive deliverable evidence for review."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "model", "kind": "file", "target": "src/models.py"}],
        },
    )
    tid = created["created"]
    assert (await db.get_task(tid)).deliverables == [
        {"id": "model", "kind": "file", "target": "src/models.py"}
    ]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.deliverables == [
        {
            "id": "model",
            "kind": "file",
            "target": "src/models.py",
            "met": True,
            "reason": "",
        }
    ]


@pytest.mark.asyncio
async def test_close_rejects_unmet_deliverable_without_an_explicit_reason(handler, db):
    """A worker cannot silently pass a task with a declared missing file."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "missing", "kind": "file", "target": "does-not-exist.py"}],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is False
    assert result["code"] == "deliverables.unmet"
    assert "missing" in result["error"]
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS
    assert await db.get_task_completion(tid) is None


@pytest.mark.asyncio
async def test_close_records_an_explicit_reason_for_an_unmet_deliverable(handler, db):
    """A deliberate scope exception is allowed but cannot be hidden from review."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "missing", "kind": "file", "target": "does-not-exist.py"}],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "deliverable_unmet": ["missing: explicitly deferred to the follow-up task"],
        },
    )

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.deliverables == [
        {
            "id": "missing",
            "kind": "file",
            "target": "does-not-exist.py",
            "met": False,
            "reason": "explicitly deferred to the follow-up task",
        }
    ]


@pytest.mark.asyncio
async def test_close_accepts_test_and_command_deliverables_declared_as_shell_commands(
    handler, db
):
    """A test suite declared as its ``aq test`` command line and a lint step
    declared as ``ruff check <changed files>`` are met by the recorded
    ``--test`` / ``--command`` values (regression for azure-current-84)."""
    await db.create_project(Project(id="p", name="P"))
    suite = "aq test tests/test_deliverables.py tests/test_task_close_summary_enforcement.py"
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [
                {"id": "focused-suite", "kind": "test", "target": suite},
                {"id": "ruff", "kind": "command", "target": "ruff check <changed files>"},
            ],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    refused = await handler.execute(
        "task_close", {"task_id": tid, "outcome": "pass", "tests": [suite]}
    )
    assert refused["success"] is False
    assert refused["code"] == "deliverables.unmet"
    assert [item["id"] for item in refused["unmet_deliverables"]] == ["ruff"]

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "tests": [suite],
            "commands": ["ruff check src/deliverables.py"],
        },
    )

    assert result["success"] is True, result
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert [(item["id"], item["met"]) for item in completion.deliverables] == [
        ("focused-suite", True),
        ("ruff", True),
    ]


# ---------------------------------------------------------------------------
# Review deliverables: a proposal goes to Reviews, not only a branch
# (vivid-delta; research task crisp-orbit-37 committed its proposal and was
# never seen in the Reviews tab).
# ---------------------------------------------------------------------------


async def _submitted_review(db, review_id, *, task_id, kind="spec", state="in_review"):
    """Store a document review with its first revision, as ``review_submit`` would."""
    import hashlib
    import time

    now = time.time()
    async with db.immediate() as conn:
        await db.insert_review(
            review={
                "id": review_id,
                "project_id": "p",
                "author_task_id": task_id,
                "kind": kind,
                "title": "Proposal",
                "vault_path": f"projects/p/specs/{review_id}.md",
                "current_revision": 1,
                "state": state,
                "gate_id": None,
                "decider": "user",
                "decided_by": None,
                "decided_at": None,
                "decision_note": None,
                "notified_revision": 0,
                "created_at": now,
                "updated_at": now,
            },
            revision={
                "review_id": review_id,
                "revision": 1,
                "content": "# Proposal\n",
                "content_sha256": hashlib.sha256(b"# Proposal\n").hexdigest(),
                "submitted_by": "session:author",
                "submitted_task_id": task_id,
                "changes_note": None,
                "submitted_at": now,
                "responder_class": None,
                "responder_profile": None,
                "responder_profile_source": None,
                "playbook": None,
                "playbook_artifact": None,
            },
            conn=conn,
        )


async def _started(handler, db, **fields) -> str:
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute("create_task", {"project_id": "p", "title": "t", **fields})
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")
    return tid


@pytest.mark.asyncio
async def test_research_close_without_a_submitted_review_is_refused(handler, db):
    """A research task's document must reach Reviews before a passing close."""
    tid = await _started(handler, db, task_type="research")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is False
    assert result["code"] == "deliverables.unmet"
    assert result["unmet_deliverables"] == [
        {"id": "review", "kind": "review", "target": "any", "met": False, "reason": ""}
    ]
    assert f"aq review submit --task-id {tid}" in result["error"]
    assert "--deliverable-unmet 'review: <reason>'" in result["error"]
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS
    assert await db.get_task_completion(tid) is None


@pytest.mark.asyncio
async def test_design_close_passes_once_its_review_is_submitted(handler, db):
    tid = await _started(handler, db, task_type="design")
    await _submitted_review(db, "rev-bright-harbor", task_id=tid)

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True, result
    completion = await db.get_task_completion(tid)
    assert completion.deliverables == [
        {"id": "review", "kind": "review", "target": "any", "met": True, "reason": ""}
    ]


@pytest.mark.asyncio
async def test_research_without_a_document_waives_the_review_visibly(handler, db):
    tid = await _started(handler, db, task_type="research")

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "deliverable_unmet": ["review: findings recorded as task comments; no document"],
        },
    )

    assert result["success"] is True, result
    completion = await db.get_task_completion(tid)
    assert completion.deliverables == [
        {
            "id": "review",
            "kind": "review",
            "target": "any",
            "met": False,
            "reason": "findings recorded as task comments; no document",
        }
    ]


@pytest.mark.asyncio
async def test_a_failed_research_close_is_never_held_for_a_review(handler, db):
    tid = await _started(handler, db, task_type="research")

    result = await handler.execute(
        "task_close", {"task_id": tid, "outcome": "fail", "summary": "could not finish"}
    )

    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_a_dispatched_adversarial_reviewer_closes_without_its_own_review(handler, db):
    """Dispatched reviewers are research-typed but answer with comments."""
    tid = await _started(handler, db, task_type="research")
    await _submitted_review(db, "rev-bright-harbor", task_id="author-task")
    async with db.immediate() as conn:
        await db.insert_review_dispatch(
            {
                "id": "dsp-1",
                "review_id": "rev-bright-harbor",
                "profile_id": "reviewer",
                "revision": 1,
                "with_comments": False,
                "focus": None,
                "task_id": tid,
                "dispatched_by": "human:local-operator",
                "created_at": 1.0,
            },
            conn=conn,
        )

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True, result
    assert (await db.get_task_completion(tid)).deliverables == []


@pytest.mark.asyncio
async def test_a_declared_review_item_needs_a_review_of_its_kind(handler, db):
    """A feature task created with a 'return a proposal' deliverable."""
    tid = await _started(
        handler,
        db,
        task_type="feature",
        deliverables=[{"id": "proposal", "kind": "review", "target": "spec"}],
    )
    await _submitted_review(db, "rev-other-kind", task_id=tid, kind="other")

    refused = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})
    assert refused["code"] == "deliverables.unmet"
    assert "--kind spec" in refused["error"]

    await _submitted_review(db, "rev-spec-kind", task_id=tid, kind="spec")
    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})
    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_a_withdrawn_review_does_not_satisfy_the_close(handler, db):
    tid = await _started(handler, db, task_type="research")
    await _submitted_review(db, "rev-withdrawn", task_id=tid, state="withdrawn")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["code"] == "deliverables.unmet"


# ---------------------------------------------------------------------------
# Subtasks: a close refusal for open checklist rows (C3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_refuses_a_completing_outcome_with_open_subtasks_no_side_effects(
    handler, db
):
    """The task carries no side effect from a refused close: it can retry."""
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}, {"title": "Second"}])
    await db.update_task_subtask("t1", 1, status="done")

    result = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})

    assert result["success"] is False
    assert result["code"] == "subtasks.open"
    assert result["open_subtasks"] == [2]
    assert "aq task subtask-done N" in result["error"]
    assert "aq task subtask-skip N --note" in result["error"]
    assert "--skip-open-subtasks" in result["error"]
    assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
    assert await db.get_task_completion("t1") is None
    subtasks = await db.list_task_subtasks("t1")
    assert subtasks[1]["status"] == "pending"


@pytest.mark.asyncio
async def test_close_with_skip_open_subtasks_flag_settles_and_succeeds(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}, {"title": "Second"}])

    result = await handler.execute(
        "task_close", {"task_id": "t1", "outcome": "pass", "skip_open_subtasks": True}
    )

    assert result["success"] is True, result
    subtasks = await db.list_task_subtasks("t1")
    assert [item["status"] for item in subtasks] == ["skipped", "skipped"]
    assert all(item["note"] == "skipped at close" for item in subtasks)
    assert ("task.subtasks_updated", {
        "task_id": "t1", "project_id": "p", "title": "t", "total": 2, "settled": 2,
    }) in handler.orchestrator.bus.events


@pytest.mark.asyncio
async def test_close_skip_rolls_back_with_the_terminal_transition_on_database_error(
    handler, db, monkeypatch
):
    """A checklist write failure cannot leave a completed task with open rows."""
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}, {"title": "Second"}])

    async def fail_skip(*_args, **_kwargs):
        raise RuntimeError("injected checklist write failure")

    monkeypatch.setattr(db, "_skip_open_task_subtasks_on", fail_skip)
    result = await handler.execute(
        "task_close", {"task_id": "t1", "outcome": "pass", "skip_open_subtasks": True}
    )

    assert result.get("success") is not True
    assert "injected checklist write failure" in result["error"]
    assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
    assert [row["status"] for row in await db.list_task_subtasks("t1")] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_skip_open_subtasks_writes_nothing_when_a_later_refusal_wins(handler, db):
    """The flip must land after the LAST refusal point, not before the first.

    ``--skip-open-subtasks`` used to commit the moment the flag was seen,
    so a close that the open-children gate (or any other later check) then
    refused left every row ``skipped`` with its note overwritten -- and the
    retry had nothing left to settle.
    """
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.create_task(
        Task(id="t1.1", project_id="p", title="c", description="d", parent_task_id="t1")
    )
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}, {"title": "Second"}])
    await db.update_task_subtask("t1", 1, status="in_progress", note="halfway")

    result = await handler.execute(
        "task_close", {"task_id": "t1", "outcome": "pass", "skip_open_subtasks": True}
    )

    assert result["success"] is False
    assert result["code"] == "hierarchy.open_children"
    subtasks = await db.list_task_subtasks("t1")
    assert [item["status"] for item in subtasks] == ["in_progress", "pending"]
    assert subtasks[0]["note"] == "halfway"
    assert not [e for e in handler.orchestrator.bus.events if e[0] == "task.subtasks_updated"]


@pytest.mark.asyncio
async def test_skip_open_subtasks_never_overwrites_an_existing_note(handler, db):
    """A note the worker wrote is evidence; ``skipped at close`` is a default."""
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}, {"title": "Second"}])
    await db.update_task_subtask("t1", 1, note="blocked on upstream fix")

    result = await handler.execute(
        "task_close", {"task_id": "t1", "outcome": "pass", "skip_open_subtasks": True}
    )

    assert result["success"] is True, result
    subtasks = await db.list_task_subtasks("t1")
    assert [item["status"] for item in subtasks] == ["skipped", "skipped"]
    assert subtasks[0]["note"] == "blocked on upstream fix"
    assert subtasks[1]["note"] == "skipped at close"


@pytest.mark.asyncio
async def test_close_with_no_open_subtasks_needs_no_flag(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}])
    await db.update_task_subtask("t1", 1, status="done")

    result = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})

    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_failing_close_is_never_refused_for_open_subtasks(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    await db.add_task_subtasks("t1", "p", [{"title": "First"}])

    result = await handler.execute("task_close", {"task_id": "t1", "outcome": "fail"})

    assert result["success"] is True, result
    subtasks = await db.list_task_subtasks("t1")
    assert subtasks[0]["status"] == "pending"
