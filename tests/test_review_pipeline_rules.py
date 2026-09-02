"""Tests for the per-task review rule in the default pipeline playbook (T4).

Verifies:
1. The default-pipeline.md compiles without errors via compile_playbook.
2. A ``scope: task`` rule with ``create_task`` + ``profile_id: reviewer`` exists.
3. Dispatching ``task.completed`` for a task WITH a branch creates a reviewer
   task, links it ``discovered-from`` the reviewed task, and attaches a ``task``
   gate to every downstream dependent.
4. Dispatching ``task.completed`` for a task WITHOUT a branch is a no-op.
5. Dispatching the same event_id twice (idempotency) creates at most one
   reviewer task.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project
from src.orchestrator import Orchestrator
from src.orchestrator.core import _eval_pipeline_when

from tests.conftest import DEFAULT_PIPELINE_PATH as _DEFAULT_PIPELINE

# ``command_handler_factory`` and ``pipeline_engine_factory`` fixtures, plus
# the ``PipelineEngine`` test helper, live in tests/conftest.py — shared with
# test_review_pipeline_e2e.py and test_review_reopen_cascade.py.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "rv.db"))
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "rv.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    o.bus = MagicMock()
    o.bus.emit = AsyncMock()
    return CommandHandler(o, config)


# ---------------------------------------------------------------------------
# T1: parse test
# ---------------------------------------------------------------------------


def test_eval_pipeline_when_all_clause_requires_every_field():
    """The ``all`` clause (used by per-branch-final-review) requires every
    nested field to be truthy — branch_name alone is not enough once pr_url
    is also required, but back-compat single-field ``when`` still works.
    """
    when = {
        "all": [
            {"field": "event.task.branch_name", "truthy": True},
            {"field": "event.task.pr_url", "truthy": True},
        ]
    }

    both_truthy = {"task": {"branch_name": "feature/x", "pr_url": "https://x/pr/1"}}
    assert _eval_pipeline_when(when, both_truthy) is True

    missing_pr = {"task": {"branch_name": "feature/x", "pr_url": ""}}
    assert _eval_pipeline_when(when, missing_pr) is False

    missing_pr_key = {"task": {"branch_name": "feature/x"}}
    assert _eval_pipeline_when(when, missing_pr_key) is False

    missing_branch = {"task": {"branch_name": "", "pr_url": "https://x/pr/1"}}
    assert _eval_pipeline_when(when, missing_branch) is False

    # Back-compat: a plain single-field ``when`` (no all/any) still works.
    single = {"field": "event.task.branch_name", "truthy": True}
    assert _eval_pipeline_when(single, both_truthy) is True
    assert _eval_pipeline_when(single, {"task": {"branch_name": ""}}) is False


def test_compile_rejects_empty_all_clause():
    """{'all': []} evaluates vacuously True — must be rejected at compile."""
    from src.playbooks.pipeline_compiler import compile_pipeline

    md = """---
id: bad-empty-all
kind: pipeline
scope: system
role: pipeline
triggers: [task.completed]
---
```json
{
  "rules": [
    {
      "id": "r1",
      "on": "task.completed",
      "when": {"all": []},
      "entry": "n1",
      "nodes": {"n1": {"command": "list_tasks", "on_success": "done"}, "done": {"terminal": true}}
    }
  ]
}
```
"""
    result = compile_pipeline(md)
    assert result.errors, "expected compile error for empty when.all"
    assert any("when.all" in str(e) for e in result.errors)


def test_compile_rejects_empty_any_clause():
    from src.playbooks.pipeline_compiler import compile_pipeline

    md = """---
id: bad-empty-any
kind: pipeline
scope: system
role: pipeline
triggers: [task.completed]
---
```json
{
  "rules": [
    {
      "id": "r1",
      "on": "task.completed",
      "when": {"any": []},
      "entry": "n1",
      "nodes": {"n1": {"command": "list_tasks", "on_success": "done"}, "done": {"terminal": true}}
    }
  ]
}
```
"""
    result = compile_pipeline(md)
    assert result.errors, "expected compile error for empty when.any"
    assert any("when.any" in str(e) for e in result.errors)


def test_per_task_review_rule_parses():
    """default-pipeline.md must compile with no errors and contain the review rule."""
    from src.playbooks.compiler import compile_playbook

    src = _DEFAULT_PIPELINE.read_text(encoding="utf-8")
    compiled = compile_playbook(src)
    assert compiled.errors == [], compiled.errors

    # The pipeline_rules must map task.completed → per-task-review entry.
    pb = compiled.playbook
    assert pb is not None
    assert "task.completed" in pb.pipeline_rules, (
        f"pipeline_rules missing 'task.completed'; got: {pb.pipeline_rules}"
    )

    # There must be a node that calls create_task / ensure_task with reviewer profile.
    reviewer_create_found = any(
        node.action
        and node.action.get("command") in ("create_task", "ensure_task")
        and "reviewer" in str(node.action.get("args", {}))
        for node in pb.nodes.values()
    )
    assert reviewer_create_found, (
        "No ensure_task/create_task node with 'reviewer' in args found in compiled nodes"
    )


def _task_completed_rule_whens() -> dict[str, dict]:
    """``rule id -> when`` for every rule the default pipeline hangs on task.completed."""
    from src.playbooks.compiler import compile_playbook

    compiled = compile_playbook(_DEFAULT_PIPELINE.read_text(encoding="utf-8"))
    assert compiled.errors == [], compiled.errors
    metas = compiled.playbook.pipeline_rules["task.completed"]
    if isinstance(metas, (str, dict)):
        metas = [metas]
    return {m["entry"]: m["when"] for m in metas}


def test_review_rules_skip_no_code_completions():
    """Both review rules must stand down for a task that produced no code.

    The close path sets ``no_code: true`` for a ``read_only`` profile or a
    ``--work-outcome no-op`` close.  A reviewer task has a ``branch_name``
    like any other session task (its slot is on ``aq/<id>``), so without this
    guard ``per-task-review`` reviewed every finished review, recursively.
    An emitter that omits the key must still fire — the guard only narrows.
    """
    whens = _task_completed_rule_whens()
    review_when = next(w for e, w in whens.items() if e.startswith("per-task-review-"))
    final_when = next(w for e, w in whens.items() if e.startswith("per-branch-final-review-"))

    hydrated = {"task": {"branch_name": "aq/r-1", "pr_url": "https://github.com/o/r/pull/1"}}

    assert _eval_pipeline_when(review_when, {**hydrated, "no_code": True}) is False
    assert _eval_pipeline_when(final_when, {**hydrated, "no_code": True}) is False

    assert _eval_pipeline_when(review_when, {**hydrated, "no_code": False}) is True
    assert _eval_pipeline_when(final_when, {**hydrated, "no_code": False}) is True

    # Key absent (container settlement, hand-written events): code-bearing.
    assert _eval_pipeline_when(review_when, hydrated) is True
    assert _eval_pipeline_when(final_when, hydrated) is True


def test_review_rules_skip_the_pipelines_own_review_tasks():
    """Both review rules must stand down for a task the pipeline created as a review.

    ``no_code`` is only as good as the reviewer profile's ``read_only`` flag —
    an operator who gives the reviewer Write/Edit tools disarms it and the
    recursion is back.  The close path therefore also sets ``review_task`` from
    the task's own ``review:task:`` / ``branch-review:`` dedup key, which no
    profile edit can change.  Absent key still fires — the guard only narrows.
    """
    whens = _task_completed_rule_whens()
    review_when = next(w for e, w in whens.items() if e.startswith("per-task-review-"))
    final_when = next(w for e, w in whens.items() if e.startswith("per-branch-final-review-"))

    hydrated = {"task": {"branch_name": "aq/r-1", "pr_url": "https://github.com/o/r/pull/1"}}

    flagged = {**hydrated, "no_code": False, "review_task": True}
    assert _eval_pipeline_when(review_when, flagged) is False
    assert _eval_pipeline_when(final_when, flagged) is False

    plain = {**hydrated, "no_code": False, "review_task": False}
    assert _eval_pipeline_when(review_when, plain) is True
    assert _eval_pipeline_when(final_when, plain) is True

    assert _eval_pipeline_when(review_when, {**hydrated, "no_code": False}) is True
    assert _eval_pipeline_when(final_when, {**hydrated, "no_code": False}) is True


# ---------------------------------------------------------------------------
# T2: fires on completion with branch
# ---------------------------------------------------------------------------


async def test_per_task_review_fires_on_completion_with_branch(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))

    # Reviewed task + one downstream dependent.
    t1_res = await h.execute("create_task", {"project_id": "p", "title": "T1", "profile_id": "worker"})
    t1 = t1_res["created"]
    t2_res = await h.execute("create_task", {"project_id": "p", "title": "T2", "profile_id": "worker"})
    t2 = t2_res["created"]
    await h.execute("add_dependency", {"task_id": t2, "depends_on": t1, "dep_type": "blocks"})
    await db.update_task(t1, branch_name="feature/t1")

    # Fire the pipeline reaction for task.completed on t1.
    await engine.dispatch("task.completed", {"task_id": t1, "project_id": "p", "title": "T1"})

    # A review task exists, discovered-from t1, profile=reviewer.
    all_tasks = await db.list_tasks(project_id="p")
    reviews = [t for t in all_tasks if t.profile_id == "reviewer"]
    assert len(reviews) == 1, f"Expected 1 reviewer task, got {len(reviews)}: {[(t.id, t.title) for t in reviews]}"

    # get_typed_dependencies returns (depends_on_task_id, dep_type) pairs for the review task.
    deps = await db.get_typed_dependencies(reviews[0].id)
    assert any(
        dep_type == "discovered-from" and dep_id == t1 for dep_id, dep_type in deps
    ), f"No discovered-from → {t1} edge on review task; deps: {deps}"

    # A ``task`` gate is attached to t2 awaiting the review's completion.
    gates = await db.get_gates_for_task(t2)
    assert any(
        g["gate_type"] == "task" and g.get("await_id") == reviews[0].id for g in gates
    ), f"No task gate on {t2} awaiting {reviews[0].id}; gates: {gates}"


# ---------------------------------------------------------------------------
# T3: skips when no branch
# ---------------------------------------------------------------------------


async def test_per_task_review_skips_when_no_branch(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="chat", name="Chat"))

    t_res = await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "chat"})
    t = t_res["created"]
    # No branch_name set on the task — it stays None.

    await engine.dispatch("task.completed", {"task_id": t, "project_id": "p", "title": "T"})

    all_tasks = await db.list_tasks(project_id="p")
    assert all(x.profile_id != "reviewer" for x in all_tasks), (
        "review must not spawn for branchless tasks; "
        f"got tasks: {[(x.id, x.profile_id) for x in all_tasks]}"
    )


# ---------------------------------------------------------------------------
# T4: idempotency
# ---------------------------------------------------------------------------


async def test_per_task_review_is_idempotent(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))

    t_res = await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "worker"})
    t = t_res["created"]
    await db.update_task(t, branch_name="feature/t")

    payload = {"task_id": t, "project_id": "p", "title": "T"}
    # Dispatching the same event_id twice must not create two reviews.
    await engine.dispatch("task.completed", payload, event_id="evt-1")
    await engine.dispatch("task.completed", payload, event_id="evt-1")

    all_tasks = await db.list_tasks(project_id="p")
    reviews = [x for x in all_tasks if x.profile_id == "reviewer"]
    assert len(reviews) == 1, (
        f"Expected exactly 1 reviewer task after idempotent dispatch, got {len(reviews)}"
    )


# ---------------------------------------------------------------------------
# T5: per-branch final review coalesces via ensure_task
# ---------------------------------------------------------------------------


async def test_per_branch_review_ensures_one_task_per_branch(
    command_handler_factory, pipeline_engine_factory
):
    """Two per-branch dispatches on the same branch produce exactly one
    final-review task, and each per-task review is wired ``blocks`` → final.
    """
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))

    ta = (
        await h.execute(
            "create_task", {"project_id": "p", "title": "A", "profile_id": "worker"}
        )
    )["created"]
    tb = (
        await h.execute(
            "create_task", {"project_id": "p", "title": "B", "profile_id": "worker"}
        )
    )["created"]
    await db.update_task(
        ta, branch_name="feature/shared", pr_url="https://github.com/o/r/pull/1"
    )
    await db.update_task(
        tb, branch_name="feature/shared", pr_url="https://github.com/o/r/pull/1"
    )

    await engine.dispatch(
        "task.completed", {"task_id": ta, "project_id": "p", "title": "A"}, event_id="e-a"
    )
    await engine.dispatch(
        "task.completed", {"task_id": tb, "project_id": "p", "title": "B"}, event_id="e-b"
    )

    tasks = await db.list_tasks(project_id="p")
    finals = [t for t in tasks if t.profile_id == "final-reviewer"]
    assert len(finals) == 1, (
        f"expected one final-review task coalesced by ensure_task; got {len(finals)}: "
        f"{[(t.id, t.title) for t in finals]}"
    )
    reviews = [t for t in tasks if t.profile_id == "reviewer"]
    assert len(reviews) == 2, f"expected 2 per-task reviews, got {len(reviews)}"

    # Each per-task review blocks the final review.
    final_deps = await db.get_typed_dependencies(finals[0].id)
    review_ids = {r.id for r in reviews}
    blocking = {dep_id for dep_id, dep_type in final_deps if dep_type == "blocks"}
    assert review_ids <= blocking, (
        f"final review {finals[0].id} must be blocked by every per-task review; "
        f"got deps={final_deps}, expected all of {review_ids}"
    )


async def test_per_branch_review_gates_downstream_with_pr_merged(
    command_handler_factory, pipeline_engine_factory
):
    """When the reviewed task has a pr_url, every downstream dependent gains
    a ``pr-merged`` gate whose ``await_id`` is that PR URL.
    """
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))

    t = (
        await h.execute(
            "create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}
        )
    )["created"]
    dep = (
        await h.execute(
            "create_task", {"project_id": "p", "title": "Dep", "profile_id": "worker"}
        )
    )["created"]
    await h.execute(
        "add_dependency", {"task_id": dep, "depends_on": t, "dep_type": "blocks"}
    )
    await db.update_task(
        t, branch_name="feature/x", pr_url="https://github.com/o/r/pull/9"
    )

    await engine.dispatch(
        "task.completed", {"task_id": t, "project_id": "p", "title": "T"}
    )

    gates = await db.get_gates_for_task(dep)
    assert any(
        g["gate_type"] == "pr-merged"
        and g.get("await_id") == "https://github.com/o/r/pull/9"
        for g in gates
    ), f"expected pr-merged gate on {dep} awaiting PR URL; got: {gates}"


# ---------------------------------------------------------------------------
# T6: a review task is recognised structurally, not by the emitter's flag
# ---------------------------------------------------------------------------


async def test_per_task_review_skips_a_review_task_when_the_emitter_omits_the_flag(
    command_handler_factory, pipeline_engine_factory
):
    """Hydration derives ``review_task`` from the row's ``review:task:`` key.

    The ``when`` guard alone only narrows on a key the emitter set; the
    orchestrator (and this helper, which mirrors it) must flag the event from
    the task row so a slim ``task.completed`` for a finished review never
    spawns ``Review: Review: ...``.
    """
    h = await command_handler_factory()
    engine = pipeline_engine_factory(handler=h)
    db = h.db

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))

    t1 = (await h.execute("create_task", {"project_id": "p", "title": "T1", "profile_id": "worker"}))["created"]
    await db.update_task(t1, branch_name="feature/t1")
    await engine.dispatch("task.completed", {"task_id": t1, "project_id": "p", "title": "T1"})
    reviews = [t for t in await db.list_tasks(project_id="p") if t.profile_id == "reviewer"]
    assert len(reviews) == 1
    review = reviews[0]
    assert review.dedup_key == f"review:task:{t1}"

    # The review finishes on its own slot branch; the emitter sends no flags.
    await db.update_task(review.id, branch_name=f"aq/{review.id}")
    await engine.dispatch(
        "task.completed", {"task_id": review.id, "project_id": "p", "title": review.title}
    )

    reviews_after = [t for t in await db.list_tasks(project_id="p") if t.profile_id == "reviewer"]
    assert [t.id for t in reviews_after] == [review.id], (
        f"review of the review spawned: {[(t.id, t.title) for t in reviews_after]}"
    )
