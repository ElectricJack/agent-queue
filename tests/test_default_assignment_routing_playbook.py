"""The shipped router runs as an ordinary pipeline over the router commands.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`` §6.7.
The orchestrator only emits ``task.route_needed``; the reviewed artifact plans
with ``task_route_plan`` over its policy block, asks the LLM to classify only
when the plan needs an answer, and writes the plan with ``task_route_apply``.
These run the reviewed fixture through the real engine and a real handler, so
the invocation ``task_route_apply`` checks is the artifact's own.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Project, Task, TaskStatus, TaskType
from src.orchestrator import Orchestrator
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS, ExecutionMode
from src.playbooks.executors.base import EngineServices
from src.playbooks.executors.llm import _result as llm_result
from src.profiles.capabilities import CapabilityPolicy
from src.routing.sources import LEGACY, ROUTER, UNROUTED
from src.sessions.harness_parser import Harness
from tests.db_fixtures import lease_dsn
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

FIXTURE = Path("tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json")
ROUTER_ID = "default-assignment-routing"

CLASSES = {
    class_id: IntelligenceClass(class_id, class_id, "", {
        "anthropic": {"model": f"claude-{class_id}"},
        "codex": {"model": f"gpt-{class_id}"},
    })
    for class_id in ("standard-high", "deep-low", "deep-high")
}

NARROW_DESIGN = {
    "task_type": "design", "intelligence_class": "deep-high", "narrow": False,
    "test_verified": False, "independent_verifier": False,
    "reason": "asks for an architecture",
}


class _ScriptedLlm:
    """Stands in for the live LLM executor: answers the classify step."""

    def __init__(self, answer: dict | None, outcome: str = "completed") -> None:
        self.answer = answer
        self.outcome = outcome
        self.prompts: list[str] = []
        self.inputs: dict | None = None

    async def execute(self, step, ctx):
        self.prompts.append(step.prompt.value)
        self.inputs = dict(ctx.inputs)
        return llm_result(step, ctx, outcome=self.outcome, value=self.answer)


def _profiles() -> list[AgentProfile]:
    return [
        AgentProfile(id="playbook-compiler", name="compiler", harness="claude"),
        AgentProfile(id="standard-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="standard-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="deep-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="deep-high", max_active=1),
        AgentProfile(id="deep-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="deep-high", max_active=1),
    ]


@pytest.fixture
async def handler(tmp_path):
    from src.commands.handler import CommandHandler

    db = Database(lease_dsn("routing-playbook.db"))
    await db.initialize()
    for profile in _profiles():
        await db.create_profile(profile)
    await db.create_project(Project(id="p", name="Project"))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("unused.db")),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orchestrator = Orchestrator(cfg)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus.emit = AsyncMock()
    for harness in ("claude", "codex"):
        orchestrator.harness_registry.upsert(Harness(
            id=harness, name=harness, command=harness, model_flag="--model",
        ))
    orchestrator.session_spec_builder._intelligence_classes = dict(CLASSES)
    orchestrator.intelligence_classes.replace(dict(CLASSES))
    handler = CommandHandler(orchestrator, cfg)
    set_handler_provider(lambda: handler)
    try:
        yield handler
    finally:
        set_handler_provider(None)
        await db.close()


def _engine(handler):
    artifact = load_definition_json(FIXTURE.read_text(encoding="utf-8"))
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 123.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=handler.db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([artifact_ref_for(artifact)]),
    )
    return engine, runs


def _principal():
    return ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["task_route_plan", "task_route_apply"]
        ),
    )


def _event(task: Task, *, router: str = ROUTER_ID) -> dict:
    return {
        "event_type": "task.route_needed",
        "event_id": f"route-{task.id}",
        "task_id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "router": router,
    }


async def _create(handler, task_id: str, **kw) -> Task:
    kw.setdefault("status", TaskStatus.READY)
    await handler.db.create_task(Task(
        id=task_id, project_id="p", title=task_id, description="d", **kw,
    ))
    return await handler.db.get_task(task_id)


async def _dispatch(handler, task: Task, llm: _ScriptedLlm, monkeypatch, **event):
    monkeypatch.setitem(EXECUTORS[ExecutionMode.LIVE], "llm", llm)
    engine, runs = _engine(handler)
    result = await engine.dispatch_event(_event(task, **event), _principal())
    return result, runs


def _steps(runs) -> list[str]:
    return [receipt.step_id for receipt in runs.receipts]


async def test_a_task_whose_kind_and_hint_decide_the_route_needs_no_llm(
    handler, monkeypatch,
):
    """Acceptance: the kind and the hint decide, so no classification is asked.

    Under the shipped ``risk`` table that takes a class at or above every risk
    floor, with every candidate already on Claude or Codex: no risk answer
    could change the route.
    """
    task = await _create(handler, "hinted", task_type=TaskType.BUGFIX, class_hint="deep-high")
    llm = _ScriptedLlm(NARROW_DESIGN)

    result, runs = await _dispatch(handler, task, llm, monkeypatch)

    assert tuple(result.rules_selected) == ("route-task",)
    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert llm.prompts == []
    steps = _steps(runs)
    assert "route-task--plan_first" in steps and "route-task--apply_a" in steps
    assert "route-task--classify" not in steps
    routed = await handler.db.get_task("hinted")
    assert routed.route_source == ROUTER
    # ``reserved`` keeps deep-high Claude for design, so Codex takes it.
    assert (routed.profile_id, routed.intelligence_class) == ("deep-high-codex", "deep-high")
    assert routed.route["rule"] == "kinds.bugfix"
    assert routed.route["classification"] is None


@pytest.mark.parametrize(
    ("answer", "routed_to", "raised"),
    [
        # Low risk has no rule: the hint stands.
        ({"risk": "low"}, ("standard-high-codex", "standard-high"), None),
        # Very high risk floors at deep-low, beating the hint...
        ({"risk": "very_high"}, ("deep-low-codex", "deep-low"),
         {"from": "standard-high", "to": "deep-low", "risk": "very_high"}),
        # ...unless the change is narrow and test-verified.
        ({"risk": "very_high", "narrow": True, "test_verified": True},
         ("standard-high-codex", "standard-high"), None),
    ],
    ids=["low", "very_high", "very_high_narrow_tested"],
)
async def test_a_hinted_task_below_the_top_risk_floor_is_classified_for_its_risk(
    handler, monkeypatch, answer, routed_to, raised,
):
    """Risk-aware routing: a risk floor could raise the hint, so the risk is asked.

    The raise travels from ``task_route_plan`` through the playbook's binding
    into the route record ``task_route_apply`` writes.
    """
    for harness in ("claude", "codex"):
        await handler.db.create_profile(AgentProfile(
            id=f"deep-low-{harness}", name="", lifecycle="pool", harness=harness,
            default_class="deep-low", max_active=1,
        ))
    task = await _create(handler, "risky", task_type=TaskType.BUGFIX, class_hint="standard-high")
    llm = _ScriptedLlm({
        **NARROW_DESIGN, "task_type": "bugfix", "intelligence_class": "standard-high",
        "risk_reason": "deletes branches on origin", **answer,
    })

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert len(llm.prompts) == 1
    # Typed and hinted: only the risk and the flags its relax reads are asked.
    assert llm.inputs["questions"] == ["narrow", "risk", "test_verified"]
    assert "route-task--apply_b" in _steps(runs)
    routed = await handler.db.get_task("risky")
    assert (routed.profile_id, routed.intelligence_class) == routed_to
    assert routed.route.get("class_raised_for_risk") == raised
    assert routed.route["classification"]["risk"] == answer["risk"]
    assert routed.route["classification"]["risk_reason"] == "deletes branches on origin"
    assert {c["harness"] for c in routed.route["candidates"]} <= {"claude", "codex"}


@pytest.mark.parametrize("origin", ["integration_repair", "development_repair", "ordinary"])
async def test_shipped_router_keeps_bugfix_repairs_off_local_and_hosted_opencode(
    handler, monkeypatch, origin,
):
    for harness in (
        "opencode", "opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat",
        "opencode-zen-new-preview",
    ):
        await handler.db.create_profile(AgentProfile(
            id=f"standard-high-{harness}", name="", lifecycle="pool", harness=harness,
            default_class="standard-high", max_active=20,
        ))
        handler.orchestrator.harness_registry.upsert(Harness(
            id=harness, name=harness, command="opencode", provider="anthropic",
        ))
    if origin == "ordinary":
        from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
        from src.integration.repair import OrdinaryRepairService
        from src.models import RepoConfig, RepoSourceType

        await handler.db.create_repo(
            RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
        )
        await BatchStore(handler.db).freeze(
            Batch("batch", "p", "repo", "refs/heads/main"),
            (BatchMember("source", "a" * 40, "b" * 40),), trees={"source": "c" * 40},
        )
        allocation = await OrdinaryRepairService(handler.db).allocate(
            "batch", target_ref=candidate_ref("batch"), head_sha="a" * 40,
            authorize=AsyncMock(return_value=True), intelligence_class="standard-high",
        )
        assert allocation["outcome"] == "filed", allocation
        task = await handler.db.get_task(allocation["task_id"])
        assert task.created_by_kind == "system"
    else:
        task = await _create(
            handler, "repair", task_type=TaskType.BUGFIX, class_hint="standard-high",
            created_by_kind=origin,
        )
    # Narrow, test-verified and low risk: the strongest case for a narrow
    # lane, which a repair origin still never takes.
    llm = _ScriptedLlm({**NARROW_DESIGN, "task_type": "bugfix", "narrow": True,
                        "test_verified": True, "risk": "low", "risk_reason": "one file"})

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    routed = await handler.db.get_task(task.id)
    assert routed.route_source == ROUTER
    effective_origin = "integration_repair" if origin == "ordinary" else origin
    assert routed.route["rule"] == f"kinds.bugfix+origins.{effective_origin}"
    assert {c["harness"] for c in routed.route["candidates"]} == {"codex", "claude"}
    assert routed.profile_id in {"standard-high-codex", "standard-high-claude"}
    # A risk floor could raise the hint, so the router asks for the risk.
    assert len(llm.prompts) == 1
    assert routed.route["classification"]["risk"] == "low"
    assert routed.created_by_kind == task.created_by_kind


async def test_a_filed_class_without_a_profile_is_still_honoured_as_the_hint(
    handler, monkeypatch,
):
    """The superseded router's ``explicit`` branch, kept for unrouted filings.

    Until creation writes ``class_hint`` (mandatory routing Task 4), a worker
    filing with ``--intelligence-class`` and no profile is stored unrouted
    with the class in ``intelligence_class``.
    """
    task = await _create(
        handler, "filed", task_type=TaskType.RESEARCH, intelligence_class="deep-high",
    )
    assert (task.route_source, task.class_hint) == (UNROUTED, None)
    llm = _ScriptedLlm(NARROW_DESIGN)

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert llm.prompts == []
    routed = await handler.db.get_task("filed")
    # Research defaults to standard-high; the filed class lifts it, and
    # ``reserved`` keeps deep-high Claude for design, so Codex takes it.
    assert (routed.route_source, routed.profile_id, routed.intelligence_class) == (
        ROUTER, "deep-high-codex", "deep-high",
    )


async def test_an_unhinted_task_is_classified_then_routed(handler, monkeypatch):
    """Classification is the only LLM step, and it only ever names a kind and a class."""
    task = await _create(handler, "open")
    llm = _ScriptedLlm(NARROW_DESIGN)

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert len(llm.prompts) == 1 and llm.prompts[0].startswith("## Classifying a task")
    # §6.5: the classifier never sees a profile, a provider, a load or the
    # task's priority.
    assert set(llm.inputs) == {
        "title", "description", "task_type", "class_hint",
        "questions", "allowed_kinds", "allowed_classes",
    }
    assert llm.inputs["title"] == "open"
    assert "design" in llm.inputs["allowed_kinds"]
    assert "route-task--apply_b" in _steps(runs)
    routed = await handler.db.get_task("open")
    # Design goes to the code-design lane, which prefers Claude.
    assert (routed.route_source, routed.profile_id, routed.intelligence_class) == (
        ROUTER, "deep-high-claude", "deep-high",
    )
    assert routed.task_type == TaskType.DESIGN
    assert routed.route["classification"]["task_type"] == "design"


@pytest.mark.parametrize(
    "llm",
    [
        _ScriptedLlm(None, outcome="runtime_error"),
        # An answer outside the policy's vocabulary is a failed classification.
        _ScriptedLlm({**NARROW_DESIGN, "task_type": "galaxy"}),
    ],
    ids=["runtime_error", "invalid_answer"],
)
async def test_a_failed_classification_still_routes_on_the_policy_defaults(
    handler, monkeypatch, llm,
):
    task = await _create(handler, "unclear")

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert len(llm.prompts) == 1
    routed = await handler.db.get_task("unclear")
    # ``default_kind: feature`` at its class, ``standard-high``.
    assert (routed.route_source, routed.profile_id, routed.intelligence_class) == (
        ROUTER, "standard-high-codex", "standard-high",
    )
    assert routed.route["rule"] == "kinds.feature"
    assert routed.route["classification"]["failed"] is True
    assert routed.task_type is None


async def test_a_legacy_route_goes_back_through_the_router(handler, monkeypatch):
    task = await _create(
        handler, "old", task_type=TaskType.RESEARCH, route_source=LEGACY,
        profile_id="standard-high-claude", intelligence_class="standard-high",
    )

    # The research kind could be raised by a risk floor, so it is classified.
    llm = _ScriptedLlm({
        **NARROW_DESIGN, "task_type": "research", "intelligence_class": "standard-high",
        "risk": "medium", "risk_reason": "reads the scheduler",
    })

    _result, runs = await _dispatch(handler, task, llm, monkeypatch)

    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert len(llm.prompts) == 1
    routed = await handler.db.get_task("old")
    assert routed.route_source == ROUTER
    assert routed.profile_id == "standard-high-codex"


async def test_a_task_the_router_already_routed_starts_no_run(handler, monkeypatch):
    task = await _create(
        handler, "done", task_type=TaskType.RESEARCH, route_source=ROUTER,
        profile_id="standard-high-claude", intelligence_class="standard-high",
    )
    llm = _ScriptedLlm(NARROW_DESIGN)

    result, runs = await _dispatch(handler, task, llm, monkeypatch)

    assert result.rules_selected == ()
    assert not runs.snapshots
    assert (await handler.db.get_task("done")).updated_at == task.updated_at


async def test_a_stale_event_for_a_finished_task_starts_no_run(handler, monkeypatch):
    """The guard reads the current row, not the route-needed payload."""
    task = await _create(handler, "finished", status=TaskStatus.COMPLETED)
    llm = _ScriptedLlm(NARROW_DESIGN)

    result, runs = await _dispatch(handler, task, llm, monkeypatch)

    assert result.rules_selected == ()
    assert not runs.snapshots
    assert llm.prompts == []
    assert (await handler.db.get_task("finished")).route_source == UNROUTED


async def test_a_project_bound_to_another_router_is_left_to_it(handler, monkeypatch):
    task = await _create(handler, "elsewhere", task_type=TaskType.RESEARCH)

    result, runs = await _dispatch(
        handler, task, _ScriptedLlm(NARROW_DESIGN), monkeypatch, router="project-router",
    )

    assert result.rules_selected == ()
    assert not runs.snapshots
    assert (await handler.db.get_task("elsewhere")).route_source == UNROUTED
