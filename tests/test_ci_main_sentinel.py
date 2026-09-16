"""Source and artifact checks for the shipped ``ci-main-sentinel`` playbook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import CiBaselineStatusValue, EnsureTaskValue
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    canonical_bytes,
    contract_fingerprint,
    load_definition_json,
    source_digest,
)

FIXTURE = Path("tests/fixtures/playbooks/v2/ci-main-sentinel")
SHIPPED = Path("src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md")
RULE = "keep-main-green"


def _definition():
    return load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))


def test_repair_task_keeps_train_promotion_authority() -> None:
    from src.commands.ci_commands import render_repair_task

    _, description = render_repair_task(
        ref="main", head_sha="a" * 40, failing_checks=["Tests (default)"],
        failing_tests=[], run_url=None, attempt=1,
    )
    assert "integration train" in description
    assert "do not merge the repair PR yourself" in description
    assert "never push to it directly" in description


def test_source_is_prose_scoped_to_the_project_on_a_fifteen_minute_timer() -> None:
    loaded = PlaybookSource.load(SHIPPED, vault_root=SHIPPED.parent)
    assert isinstance(loaded, PlaybookSource), getattr(loaded, "errors", ())
    assert loaded.frontmatter["id"] == "ci-main-sentinel"
    assert loaded.frontmatter["scope"] == "project:agent-queue"
    assert loaded.frontmatter["triggers"] == ["timer.15m"]
    assert (FIXTURE / "source.md").read_bytes() == SHIPPED.read_bytes()


def test_artifact_observes_then_repairs_or_escalates() -> None:
    definition = _definition()
    assert definition.id == "ci-main-sentinel"
    assert definition.scope.type == "project" and definition.scope.project_id == "agent-queue"
    assert [rule.id for rule in definition.rules] == [RULE]
    assert definition.rules[0].trigger.event_type == "timer.15m"

    repair_id, escalate_id = f"{RULE}--ensure_repair_task", f"{RULE}--escalate_to_human"
    record_id = f"{RULE}--record_repair"
    observe = definition.steps[f"{RULE}--read_baseline"]
    repair = definition.steps[repair_id]
    record = definition.steps[record_id]
    escalate = definition.steps[escalate_id]
    done, failed = f"{RULE}--done", f"{RULE}--failed"

    assert observe.command == "ci_baseline_status"
    assert observe.save_result_as == "baseline"
    assert observe.transitions == {
        "green": done,
        "pending": done,
        "unknown": done,
        "red": repair_id,
        "red_escalated": escalate_id,
        "rejected": failed,
        "runtime_error": failed,
    }

    assert repair.command == "ensure_task"
    inputs = {name: value.model_dump(mode="json") for name, value in repair.inputs.items()}
    assert inputs["project_id"] == {"type": "literal", "value": "agent-queue"}
    assert inputs["intelligence_class"] == {"type": "literal", "value": "deep-high"}
    assert inputs["priority"] == {"type": "literal", "value": 5}
    # The repair is created at the project root: a standing parent would own
    # its children's delivery and hold the repair off the default branch,
    # which is the one thing this playbook exists to fix (final review C1).
    assert "parent_key" not in inputs
    assert "parent_title" not in inputs
    for field in ("dedup_key", "title", "description"):
        assert inputs[field] == {"type": "binding_ref", "binding": "baseline", "path": field}
    assert repair.transitions == {
        "created": record_id, "reused": record_id, "rejected": failed, "runtime_error": failed
    }

    # Every repair, new or reused, records the failure it owns, so a partial fix
    # that shrinks the failing set does not look like a new failure next tick.
    assert record.command == "ci_repair_adopt"
    inputs = {name: value.model_dump(mode="json") for name, value in record.inputs.items()}
    assert inputs["project_id"] == {"type": "literal", "value": "agent-queue"}
    assert inputs["task_id"] == {"type": "binding_ref", "binding": "repair", "path": "task_id"}
    for field, path in (
        ("ref", "ref"),
        ("head_sha", "head_sha"),
        ("failing_tests", "repair_tests"),
        ("failing_checks", "repair_checks"),
    ):
        assert inputs[field] == {"type": "binding_ref", "binding": "baseline", "path": path}
    assert record.transitions == {
        "adopted": done,
        "recorded": done,
        "unchanged": done,
        "rejected": failed,
        "runtime_error": failed,
    }

    assert escalate.command == "escalation_create"
    inputs = {name: value.model_dump(mode="json") for name, value in escalate.inputs.items()}
    assert inputs["source_kind"] == {"type": "literal", "value": "core"}
    assert inputs["source_identity"]["path"] == "escalation_key"
    assert inputs["incident_key"]["path"] == "escalation_key"
    assert inputs["severity"] == {"type": "literal", "value": "high"}
    assert escalate.transitions == {
        "created": done, "reused": done, "rejected": failed, "runtime_error": failed
    }
    assert definition.steps[done].outcome == "completed"
    assert definition.steps[failed].outcome == "failed"


def test_every_bound_field_is_one_the_command_contract_returns() -> None:
    """The playbook binds only fields the bound steps' value contracts declare."""
    definition = _definition()
    declared = {
        "baseline": set(CiBaselineStatusValue.model_fields),
        "repair": set(EnsureTaskValue.model_fields),
    }
    for step in definition.steps.values():
        for value in getattr(step, "inputs", {}).values():
            payload = value.model_dump(mode="json")
            if payload.get("type") == "binding_ref":
                assert payload["path"] in declared[payload["binding"]], payload


def test_every_command_and_outcome_resolves_against_the_live_registry() -> None:
    definition = _definition()
    for step in definition.steps.values():
        command = getattr(step, "command", None)
        if command is None:
            continue
        registration = CONTRACTS.require(command)
        declared = {spec.name for spec in registration.contract.execution.outcomes}
        for outcome in step.transitions:
            assert outcome == "runtime_error" or outcome in declared, (command, outcome)
    assert set(definition.compiled_against.commands) == {
        "ci_baseline_status", "ci_repair_adopt", "ensure_task", "escalation_create"
    }
    assert definition.compiled_against.profiles == {}


def test_artifact_is_canonical_bound_to_the_source_and_its_manifest() -> None:
    raw = (FIXTURE / "artifact.json").read_bytes()
    definition = load_definition_json(raw.decode("utf-8"))
    source = (FIXTURE / "source.md").read_text(encoding="utf-8")
    recorded = (FIXTURE / "artifact.sha256").read_text(encoding="utf-8").strip()

    assert canonical_bytes(definition) == raw
    assert definition.source_hash == source_digest(source)
    assert recorded == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert json.loads((FIXTURE / "diagnostics.json").read_text(encoding="utf-8")) == []

    text = (FIXTURE / "manifest.md").read_text(encoding="utf-8")
    manifest = yaml.safe_load(text[4 : text.index("\n---\n", 4)])
    assert manifest["artifact_sha256"] == recorded
    assert manifest["source_sha256"] == source_digest(source)
    assert manifest["contract_fingerprint"] == contract_fingerprint(definition)
    assert manifest["capabilities_granted"]["aq_commands"] == [
        "ci_baseline_status", "ci_repair_adopt", "ensure_task", "escalation_create"
    ]


async def test_a_red_tick_files_the_repair_then_records_what_it_owns() -> None:
    """The rebuilt artifact, walked by the live engine against the real contracts.

    Proves the record step's bindings resolve: ``repair.task_id`` from
    ``ensure_task`` and the list-valued ``baseline.repair_tests`` reach
    ``ci_repair_adopt`` as its typed arguments.
    """
    from src.commands.contracts.builtin import CiRepairAdoptValue
    from src.commands.contracts.models import CommandResult
    from src.commands.principal import TRUSTED_LOCAL
    from src.playbooks.engine import PlaybookEngine
    from src.playbooks.executors.base import EngineServices
    from src.playbooks.run_state import RunLifecycle
    from tests.fixtures.contracts.engine_contracts import registry_with
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        RecordingBus,
        RecordingRunRepository,
        StubActivations,
        artifact_ref_for,
    )

    definition = _definition()
    ref = artifact_ref_for(definition)
    names = ("ci_baseline_status", "ci_repair_adopt", "ensure_task", "escalation_create")
    registry, adapter = registry_with(*(CONTRACTS.require(name).contract for name in names))
    store = InMemoryArtifactStore()
    store.put(definition)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, clock=lambda: 1_000.0, artifact_store=store, bus=RecordingBus()
        ),
        runs=runs,
        waits=None,
        activations=StubActivations([ref]),
    )
    owned = ["tests/test_c.py::test_three"]
    adapter.script(
        "ci_baseline_status",
        CommandResult(
            outcome="red",
            value=CiBaselineStatusValue(
                state="red",
                ref="main",
                head_sha="a" * 40,
                failing_checks=["Tests (default)"],
                failing_tests=["tests/test_a.py::test_one", *owned],
                dedup_key="ci-baseline:abc:1",
                title="Fix red CI on main @ aaaaaaaa (attempt 1)",
                description="d",
                in_flight=["owner"],
                repair_signature="abc",
                repair_tests=owned,
                repair_checks=["Tests (default)"],
            ),
            summary="red",
        ),
    )
    adapter.script(
        "ensure_task",
        CommandResult(
            outcome="created", value=EnsureTaskValue(task_id="repair-1", created=True), summary=""
        ),
    )
    adapter.script(
        "ci_repair_adopt",
        CommandResult(
            outcome="recorded",
            value=CiRepairAdoptValue(task_id="repair-1", dedup_key="ci-baseline:abc:1"),
            summary="",
        ),
    )

    outcome = await engine.run_rule(
        ref,
        RULE,
        {"event_id": "evt-tick", "event_type": "timer.15m", "project_id": "agent-queue"},
        TRUSTED_LOCAL,
    )

    assert outcome.lifecycle is RunLifecycle.COMPLETED
    assert adapter.names == ["ci_baseline_status", "ensure_task", "ci_repair_adopt"]
    (record,) = adapter.args_for("ci_repair_adopt")
    assert record.model_dump(exclude_none=True) == {
        "project_id": "agent-queue",
        "task_id": "repair-1",
        "ref": "main",
        "head_sha": "a" * 40,
        "failing_tests": owned,
        "failing_checks": ["Tests (default)"],
    }
