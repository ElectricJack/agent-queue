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

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project
from src.orchestrator import Orchestrator
from src.playbooks.pipeline_compiler import compile_pipeline
from src.playbooks.pipeline_runner import PipelineRunner

# ---------------------------------------------------------------------------
# Path constant
# ---------------------------------------------------------------------------

_DEFAULT_PIPELINE = (
    Path(__file__).parent.parent
    / "src"
    / "prompts"
    / "default_playbooks"
    / "default-pipeline.md"
)


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


@pytest.fixture
def command_handler_factory(tmp_path):
    """Factory that creates a fresh CommandHandler backed by a real DB."""

    async def _make():
        db = Database(str(tmp_path / "rv2.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "rv2.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        h = CommandHandler(o, cfg)
        h._db = db  # stash for teardown
        return h

    return _make


class PipelineEngine:
    """Minimal test helper that loads the default pipeline and dispatches events.

    Dispatches the compiled rule subgraph that matches the given event type,
    injecting ``event.task`` from the DB when ``task_id`` is present (mirrors
    the orchestrator hydration path).
    """

    def __init__(self, compiled, handler, db=None):
        self._compiled = compiled
        self._handler = handler
        self._db = db
        self._dispatched: set[str] = set()  # (event_type, event_id) for idempotency

    async def dispatch(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> None:
        # Idempotency: same event_id dispatched twice is a no-op.
        key = (event_type, event_id) if event_id else None
        if key and key in self._dispatched:
            return
        if key:
            self._dispatched.add(key)

        # Hydrate event.task if task_id is present.
        hydrated = dict(payload)
        hydrated["_event_type"] = event_type
        if self._db and hydrated.get("task_id") and "task" not in hydrated:
            task_row = await self._db.get_task(str(hydrated["task_id"]))
            if task_row is not None:
                from dataclasses import asdict
                try:
                    hydrated["task"] = asdict(task_row)
                except Exception:
                    hydrated["task"] = (
                        vars(task_row) if hasattr(task_row, "__dict__") else {}
                    )

        # Select rule for this event type.
        graph = self._compiled.to_dict()
        pipeline_rules = graph.get("pipeline_rules") or {}
        if not pipeline_rules:
            # Single-graph pipeline — run directly.
            runner = PipelineRunner(graph=graph, event=hydrated, handler=self._handler)
            await runner.run()
            return

        if event_type not in pipeline_rules:
            return  # No rule for this trigger

        rule_metas = pipeline_rules[event_type]
        # pipeline_rules[trigger] may be a single meta (legacy) or a list.
        if isinstance(rule_metas, (str, dict)):
            rule_metas = [rule_metas]

        import copy
        for rule_meta in rule_metas:
            if isinstance(rule_meta, str):
                rule_entry = rule_meta
                rule_when = None
            else:
                rule_entry = rule_meta.get("entry", "")
                rule_when = rule_meta.get("when")

            # Evaluate ``when`` guard.
            if rule_when:
                field_path = rule_when.get("field", "")
                val: object = hydrated
                for part in field_path.split("."):
                    if part == "event":
                        continue
                    val = val.get(part) if isinstance(val, dict) else None
                if rule_when.get("truthy") and not bool(val):
                    continue
                if rule_when.get("not_null") and (val is None or val == ""):
                    continue

            # Clone graph, set the rule's entry node.
            run_graph = copy.deepcopy(graph)
            for nid, node in run_graph["nodes"].items():
                node["entry"] = nid == rule_entry

            runner = PipelineRunner(graph=run_graph, event=hydrated, handler=self._handler)
            await runner.run()


@pytest.fixture
def pipeline_engine_factory():
    """Factory that creates a PipelineEngine from the compiled default pipeline."""

    def _make(*, handler):
        md = _DEFAULT_PIPELINE.read_text(encoding="utf-8")
        result = compile_pipeline(md)
        assert result.success, f"default-pipeline.md did not compile: {result.errors}"
        db = getattr(handler, "_db", getattr(handler.db, "_engine", None) and handler.db)
        return PipelineEngine(result.playbook, handler, db=db)

    return _make


# ---------------------------------------------------------------------------
# T1: parse test
# ---------------------------------------------------------------------------


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
    await db.update_task(ta, branch_name="feature/shared")
    await db.update_task(tb, branch_name="feature/shared")

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
