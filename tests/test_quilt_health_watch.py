"""Disabled Quilt candidate against the unmodified installed graph and old goldens.

Only the external worker and command previews are substituted. Scheduling,
collection predicates, binding, decisions and transitions use the real V2 engine.
No Quilt process, daemon mutation, message delivery or agent launch is permitted.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CONTRACTS, CommandRegistration, ContractRegistry
from src.commands.principal import TRUSTED_LOCAL, check_delegation
from src.playbooks.authority import bind, binds_run_authority
from src.playbooks.definition import TrustedExecutionMetadata, canonical_bytes, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS
from src.playbooks.executors.base import EngineServices, ExecutionMode, ExecutorResult, StepControl
from src.playbooks.executors.agent_task import narrowing_policy
from src.playbooks.expressions import ResolutionScope, evaluate_condition, resolve_value
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    artifact_ref_for,
)

FIXTURE = Path("tests/fixtures/quilt_health_watch")
CANDIDATE = Path("tests/fixtures/quilt_health_watch/candidate/quilt-health-watch")
GOLDENS = sorted((FIXTURE / "drafts").glob("dryrun-*.json"))
SCENARIOS = json.loads((FIXTURE / "equivalence-scenarios.json").read_text())


@pytest.fixture(scope="module")
def artifacts():
    return tuple(load_definition_json(path.read_text()) for path in (
        FIXTURE / "installed/artifact.json", CANDIDATE / "artifact.json",
    ))


class CheckResult:
    step_type = "agent_task"
    mode = ExecutionMode.DRY_RUN
    no_side_effects = True

    def __init__(self, outcome):
        self.outcome = outcome

    async def execute(self, step, ctx):
        return ExecutorResult(
            control=StepControl.ADVANCE, outcome=self.outcome,
            value={"task_id": "task-check", "status": self.outcome},
            operation="agent_task:scenario",
        )


async def walk(artifact, event, summary, monkeypatch, *, check="completed", read="read"):
    """Keep the historical symbolic message boundary; record its exact inputs below."""
    registry = ContractRegistry()

    async def never(*args, **kwargs):
        raise AssertionError("scenario attempted a real side effect")

    async def preview(args, principal):
        model = CONTRACTS.get("get_task").contract.execution.result_model
        value = model(
            id="task-check", project_id="quilt-trader", title="Quilt health check",
            description="(check task)", status="COMPLETED",
            completion={"outcome": "pass", "work_outcome": "no-op", "summary": summary},
        )
        return CommandResult(outcome=read, value=value, summary=read)

    for name in ("get_task", "message_send", "create_task"):
        contract = CONTRACTS.get(name).contract
        adapter = None
        if name == "get_task" and check is not None:
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"supports_preview": True}),
            })
            adapter = preview
        registry.register(CommandRegistration(
            name=name, contract=contract, invoke=never, preview=adapter,
        ))
    store = InMemoryArtifactStore()
    store.put(artifact)
    engine = PlaybookEngine(
        services=EngineServices(contracts=registry, clock=lambda: 1_790_000_000.0,
                                artifact_store=store),
        runs=RecordingRunRepository(),
    )
    with monkeypatch.context() as patch:
        if check is not None:
            patch.setitem(EXECUTORS[ExecutionMode.DRY_RUN], "agent_task", CheckResult(check))
        tree = await engine.dry_run(artifact_ref_for(artifact), event, TRUSTED_LOCAL)
    assert not tree.truncated
    return tree


def trace(tree):
    value = json.loads(json.dumps(asdict(tree)))
    # The new compiled identity is intentional; the historical trace stays unchanged.
    value.pop("artifact_sha256")
    return value


def messages(artifact, tree, event, summary):
    result = []
    bindings = {}
    for prefix in ("premarket", "market", "offhours"):
        bindings[prefix + "_check"] = {"task_id": "task-check"}
        bindings[prefix + "_report"] = {"completion": {"summary": summary}}
    scope = ResolutionScope(event=event, context={"run_id": "<run-id>"}, bindings=bindings)
    for path in tree.paths:
        for node in path.nodes:
            step = artifact.steps[node.step_id]
            if getattr(step, "command", None) == "message_send" and node.outcome == "queued":
                result.append({key: resolve_value(value, scope)
                               for key, value in step.inputs.items()})
    return result


def test_snapshot_bytes_are_unchanged():
    for line in (FIXTURE / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest, name
    assert (FIXTURE / "installed/artifact.sha256").read_text().strip() == (
        "sha256:" + hashlib.sha256((FIXTURE / "installed/artifact.json").read_bytes()).hexdigest()
    )


def test_candidate_compiles_and_only_expected_policy_changes(artifacts):
    build = runpy.run_path(str(CANDIDATE / "build.py"))
    artifact, proposal = build["compile_candidate"]()
    assert canonical_bytes(artifact) == (CANDIDATE / "artifact.json").read_bytes()
    assert not [d for d in proposal.diagnostics if d.severity in {"error", "question"}]
    old, new = artifacts
    assert set(new.required_features) == {"collections_v1"}
    assert new.version == old.version + 1
    assert new.scope == old.scope
    assert old.trusted_execution is new.trusted_execution is None
    assert new.compiled_against.profiles == {}
    assert set(new.steps) == set(old.steps)
    for name in old.steps:
        before = old.steps[name].model_dump(exclude={"source"})
        after = new.steps[name].model_dump(exclude={"source"})
        if before["type"] == "decision":
            assert before["cases"][:2] == after["cases"][:2]  # ALERT/CLEARED precedence
            before.pop("cases")
            after.pop("cases")
        assert before == after, name  # includes exact worker procedure and message inputs


def test_command_declaration_would_refuse_retained_worker_tools(artifacts):
    old, new = artifacts
    assert not binds_run_authority(old)
    assert not binds_run_authority(new)
    declared = new.model_copy(update={"trusted_execution": TrustedExecutionMetadata(
        required_capabilities=("create_task", "get_task", "message_send"),
    )})
    assert binds_run_authority(declared)
    worker = CapabilityPolicy.from_namespaces(harness_tools=["Bash", "Read", "Grep", "Glob"])
    for step_id, step in new.steps.items():
        if step.type != "agent_task":
            continue
        # These tools fit the unchanged step ceiling with an authorized caller.
        assert not check_delegation(narrowing_policy(step.capability_narrowing, worker), worker)
        effect = bind(TRUSTED_LOCAL, declared, "run-check", step_id)
        reason = check_delegation(narrowing_policy(step.capability_narrowing, effect.policy), worker)
        assert "harness tool(s) not in parent's allowlist" in reason


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.stem)
async def test_existing_dry_run_goldens_unchanged(path, artifacts, monkeypatch):
    golden = json.loads(path.read_text())
    expected = dict(golden["tree"])
    expected.pop("artifact_sha256")
    traces = []
    for artifact in artifacts:
        tree = await walk(artifact, golden["event"], golden["check_summary"], monkeypatch,
                          check=None if golden["check_summary"] is None else "completed")
        assert trace(tree) == expected
        traces.append(messages(artifact, tree, golden["event"], golden["check_summary"]))
    assert traces[0] == traces[1]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
async def test_new_equivalence_scenarios(scenario, artifacts, monkeypatch):
    traces, sent = [], []
    for artifact in artifacts:
        tree = await walk(
            artifact, scenario["event"], scenario.get("summary"), monkeypatch,
            check=scenario.get("check", "completed"), read=scenario.get("read", "read"),
        )
        assert list(tree.rules_selected) == scenario["rules"]
        traces.append(trace(tree))
        sent.append(messages(artifact, tree, scenario["event"], scenario.get("summary")))
        queued = [node.step_id.split("--")[1] for path in tree.paths for node in path.nodes
                  if node.outcome == "queued"]
        assert queued == scenario["messages"]
        if scenario.get("failed"):
            assert all(path.nodes[-1].outcome == "failed" for path in tree.paths)
    assert traces[0] == traces[1]
    assert sent[0] == sent[1]  # includes recipients, priority, subjects, full body and IDs


@pytest.mark.parametrize("day", ["2026-01-03", "2026-07-06"])
def test_every_minute_preserves_utc_window_and_complement(day, artifacts):
    # Winter weekend and summer weekday deliberately retain the UTC approximation.
    for minute in range(24 * 60):
        scope = ResolutionScope(event={"tick_time": f"{day}T{minute//60:02}:{minute%60:02}:00Z"})
        expected = 13 * 60 <= minute < 21 * 60 + 30
        for artifact in artifacts:
            rules = {r.id: r for r in artifact.rules}
            assert evaluate_condition(rules["market-hours"].guard, scope) is expected
            assert evaluate_condition(rules["off-hours"].guard, scope) is not expected
