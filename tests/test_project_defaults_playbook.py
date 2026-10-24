"""Installed/candidate project-defaults parity using real command dispatch and PostgreSQL."""

# ruff: noqa: F811 -- shared pytest fixtures are requested by name below
from __future__ import annotations

import hashlib
import json
import runpy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.commands.contracts import CONTRACTS, builtin
from src.commands.contracts.builtin import EnsureTaskValue
from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import ContractRegistry
from src.commands.principal import ExecutionPrincipal
from src.models import Project, TaskStatus
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import (
    PlaybookDefinition,
    artifact_sha256,
    canonical_bytes,
    granted_aq_commands,
)
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.private_values import PrivateValueService
from src.playbooks.receipts import transition_id
from tests.playbook_v2_engine_helpers import (
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)
from tests.test_ensure_task import config, db, handler  # noqa: F401 -- shared pytest fixtures

FIXTURE = Path("tests/fixtures/playbooks/project-defaults")
BUNDLE = Path("docs/playbooks/building-blocks/project-defaults")
STEP = "file-graft-hooks-chore--ensure_chore"
SCENARIOS = json.loads((FIXTURE / "scenarios.json").read_text())


def load(path):
    return PlaybookDefinition.model_validate_json(path.read_text())


def test_installed_recordings_are_unchanged_and_candidate_compiles():
    for line in (FIXTURE / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest
    old = load(FIXTURE / "installed/active-compiled-artifact.json")
    assert (
        artifact_sha256(old)
        == json.loads((FIXTURE / "installed/review.json").read_text())["artifact_sha256"]
    )
    new = load(BUNDLE / "artifact.json")
    assert artifact_sha256(new) == (BUNDLE / "artifact.sha256").read_text().strip()
    proposal = runpy.run_path(str(BUNDLE / "build.py"))["compile_bundle"]()
    assert not proposal.diagnostics
    rebuilt = json.loads(canonical_bytes(proposal.artifact))
    recorded = json.loads(canonical_bytes(new))
    for payload in (rebuilt, recorded):
        payload.pop("compiled_at")
    assert rebuilt == recorded
    assert granted_aq_commands(new) == granted_aq_commands(old) == {"ensure_task"}
    # The historical baseline, installed body and active graph all have the same policy.
    for path in (FIXTURE / "installed").glob("*.json"):
        payload = json.loads(path.read_text())
        if "steps" not in payload:
            continue
        for step_id, step in payload["steps"].items():
            actual = json.loads(new.steps[step_id].model_dump_json(exclude_unset=True))
            assert {k: v for k, v in actual.items() if k != "source"} == {
                k: v for k, v in step.items() if k != "source"
            }
        assert [{k: v for k, v in r.items() if k != "source"} for r in payload["rules"]] == [
            {
                k: v
                for k, v in r.model_dump(mode="json", exclude_unset=True).items()
                if k != "source"
            }
            for r in new.rules
        ]
    quoted = "\n".join(
        line[2:] if line.startswith("> ") else ""
        for line in (BUNDLE / "source.md").read_text().splitlines()
        if line.startswith(">")
    )
    assert quoted == old.steps[STEP].inputs["description"].value


@pytest.fixture(params=["installed", "candidate"])
async def scenario(request, handler, db, config, monkeypatch, tmp_path):  # noqa: F811
    path = (
        FIXTURE / "installed/active-compiled-artifact.json"
        if request.param == "installed"
        else BUNDLE / "artifact.json"
    )
    artifact = load(path)
    ref = artifact_ref_for(artifact)
    store = ArtifactStore(tmp_path / "artifacts")
    store.put(
        artifact,
        source_digest=ref.source_digest,
        contract_fingerprint=ref.contract_fingerprint,
        profile_fingerprint="",
        compiler_build=ref.compiler_build,
    )
    source_path = (
        FIXTURE / "installed/source.md" if request.param == "installed" else BUNDLE / "source.md"
    )
    store.put_source(ref.artifact_sha256, source_path.read_text())
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            handler=handler,
            db=db,
            artifact_store=store,
            private_values=private,
            clock=lambda: 1000,
        ),
        runs=runs,
        activations=StubActivations([ref]),
    )
    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    execute = AsyncMock(wraps=handler.execute)
    monkeypatch.setattr(handler, "execute", execute)
    return engine, runs, execute, handler, db, artifact


async def dispatch(scenario, event):
    engine, *_ = scenario
    return await engine.dispatch_event(event, ExecutionPrincipal.service("playbook-dispatch"))


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda c: c["name"])
async def test_event_outcomes(scenario, case, monkeypatch):
    engine, runs, execute, handler, db, artifact = scenario
    fault = case.get("fault")
    if fault == "rejected":
        monkeypatch.setattr(
            handler,
            "_cmd_ensure_task",
            AsyncMock(return_value={"success": False, "error": "scenario refusal"}),
        )
    elif fault == "runtime_error":
        execute.side_effect = RuntimeError("scenario transport failure")
    elif fault == "handler_error":
        monkeypatch.setattr(
            handler, "_cmd_ensure_task", AsyncMock(side_effect=RuntimeError("scenario"))
        )
    elif fault == "typed_rejected":
        # Exercise the authored business edge independently of the existing adapter's
        # incomplete rejected value (covered by the real-handler refusal scenarios).
        registry = ContractRegistry()
        registry.register(
            replace(
                CONTRACTS.require("ensure_task"),
                invoke=AsyncMock(
                    return_value=CommandResult(
                        outcome="rejected",
                        value=EnsureTaskValue(task_id="", created=False),
                        summary="scenario typed refusal",
                    )
                ),
            )
        )
        engine.services = replace(engine.services, contracts=registry)
    result = await dispatch(scenario, case["event"])
    if not case["selected"]:
        assert not result.run_ids
        execute.assert_not_called()
        return
    assert len(result.run_ids) == 1
    snapshot = await runs.load_run(result.run_ids[0])
    receipts = [r for r in runs.receipts if r.step_id == STEP and r.receipt_kind == "step"]
    assert snapshot.lifecycle.value == case["lifecycle"], runs.receipts
    assert [r.selected_transition for r in receipts] == [
        transition_id("file-graft-hooks-chore", STEP, case["outcome"])
    ], runs.receipts
    if fault == "typed_rejected":
        execute.assert_not_called()
        return
    execute.assert_awaited_once()
    name, args = execute.call_args.args[:2]
    assert name == "ensure_task"
    expected = {k: v.value for k, v in artifact.steps[STEP].inputs.items() if k != "project_id"}
    assert args == {**expected, "project_id": case["event"]["project_id"]}
    if case["outcome"] == "created":
        task = await db.find_task_by_dedup_key("p", "project-defaults:graft-hooks")
        assert task.description.encode() == expected["description"].encode()
        assert task.title == expected["title"]
        assert task.class_hint == "fast-high"


async def test_replay_project_isolation_and_refiling_after_completion(scenario):
    _, runs, execute, _, db, _ = scenario
    event = {
        "event_type": "project.created",
        "event_id": "first",
        "project_id": "p",
        "workspace_in_vault": False,
    }
    first = await dispatch(scenario, event)
    assert (await runs.load_run(first.run_ids[0])).lifecycle.value == "completed", runs.receipts
    task = await db.find_task_by_dedup_key("p", "project-defaults:graft-hooks")
    assert task is not None
    await dispatch(scenario, event)
    assert execute.await_count == 1  # identical durable dispatch never repeats the effect
    await dispatch(scenario, {**event, "event_id": "replay"})
    assert (await db.find_task_by_dedup_key("p", task.dedup_key)).id == task.id
    await db.create_project(Project(id="other", name="Other"))
    await dispatch(scenario, {**event, "event_id": "other", "project_id": "other"})
    other = await db.find_task_by_dedup_key("other", task.dedup_key)
    assert other is not None and other.id != task.id
    await db.transition_task(task.id, TaskStatus.COMPLETED, force=True)
    await dispatch(scenario, {**event, "event_id": "after-completion"})
    assert (await db.find_task_by_dedup_key("p", task.dedup_key)).id != task.id
    assert [
        r.selected_transition
        for r in runs.receipts
        if r.step_id == STEP and r.receipt_kind == "step"
    ] == [
        transition_id("file-graft-hooks-chore", STEP, outcome)
        for outcome in ("created", "reused", "created", "created")
    ]
