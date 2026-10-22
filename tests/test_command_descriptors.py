"""Descriptor coverage, compatibility and disabled generated execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.commands.command_inventory import command_snapshot
from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.contracts.generated import (
    GeneratedInvocationDisabled,
    JsonObjectResult,
    argument_model,
    generated_registration,
)
from src.commands.contracts.models import canonical_execution_document
from src.commands.contracts.registry import ContractRegistry
from src.commands.descriptors import (
    PLAYBOOK_DENIALS,
    HandlerCommand,
    PluginCommand,
    argument_usage,
    build_descriptor_snapshot,
    discover_handler_inventory,
)
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.mcp_registration import _discover_all_commands, effective_tool_definitions
from src.plugins.base import InternalPlugin
from src.plugins.registry import PluginRegistry
from src.tools.command_catalogue import render_command_catalogue

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/contracts/legacy-command-descriptors.json"
C2_CHANGES = ROOT / "tests/fixtures/contracts/observation-contract-changes.json"
SCRIPT_CHANGES = ROOT / "tests/fixtures/contracts/script-contract-changes.json"
MAIN_CHANGES = ROOT / "tests/fixtures/contracts/main-refresh-contract-changes.json"
DEV_CHANGES = ROOT / "tests/fixtures/contracts/dev-refresh-contract-changes.json"
WITHHELD_CHANGES = ROOT / "tests/fixtures/contracts/withheld-source-contract-changes.json"
PUBLICATION_CHANGES = ROOT / "tests/fixtures/contracts/publication-delegation-contract-changes.json"
BATCH_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/batch-repair-contract-changes.json"
WATCH_CHANGES = ROOT / "tests/fixtures/contracts/watch-contract-changes.json"
INGEST_CHANGES = ROOT / "tests/fixtures/contracts/ingestion-replay-contract-changes.json"
HIBERNATION_CHANGES = ROOT / "tests/fixtures/contracts/hibernation-contract-changes.json"
SUPERVISOR_HEALTH_CHANGES = (
    ROOT / "tests/fixtures/contracts/supervisor-health-contract-changes.json"
)
TARGET_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/target-repair-contract-changes.json"
STRUCTURE_CHANGES = ROOT / "tests/fixtures/contracts/post-release-structure-contract-changes.json"


@pytest.fixture(autouse=True)
def isolated_handler_provider():
    # Golden evidence describes offline discovery. Earlier daemon tests can
    # leave a live provider whose plugin definitions include runtime metadata.
    set_handler_provider(None)
    try:
        yield
    finally:
        set_handler_provider(None)


def _observation_changes():
    changes = json.loads(C2_CHANGES.read_text())
    assert set(changes["contracts"]) == {"list_projects", "list_tasks", "get_stuck_tasks"}
    return changes


def _script_changes():
    changes = json.loads(SCRIPT_CHANGES.read_text())
    assert set(changes["contracts"]) == {"job_logs"}
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json",
    }
    return changes


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def _main_changes():
    changes = json.loads(MAIN_CHANGES.read_text())
    assert changes["source_base"] == "c29fcd7804cef4e96bc45f3a3502ae4e8d3e70cb"
    assert set(changes["contracts"]) == {
        "gate_create", "gate_resolve", "integration_backmerge_source",
        "integration_promotion_notes_input", "integration_promotion_policy_input",
        "integration_release_owner", "promote_approve", "promote_cancel", "promote_hotfix",
        "promote_list", "promote_prepare", "promote_repair", "promote_request", "promote_status",
    }
    assert set(changes["transport_definitions"]) == {
        "add_workspace", "archive_task", "escalation_apply_reply", "gate_create", "gate_resolve",
        "integration_promotion_policy_input", "integration_release_owner", "list_workspaces",
        "message_send", "promote_request", "resume_playbook", "resume_task", "review_comment",
        "review_decide", "task_batch_propose", "task_batch_update", "task_close", "task_recover",
    }
    active = {
        "ci-main-sentinel/artifact.json", "default-assignment-routing/artifact.json",
        "default-pipeline/artifact.json", "github-issue-triage/artifact.json",
        "morning-report/artifact.json", "object-loop/artifact.json",
        "promotion-continuous/artifact.json", "promotion-request/artifact.json",
        "provider-failover/artifact.json", "provider-usage-probe/artifact.json",
        "review-pipeline.artifact.json", "review-pipeline.receipts.json",
        "review-pipeline.v6.artifact.json", "stuck-task-watch/artifact.json",
        "supervisor-failure-triage/artifact.json",
    }
    retired = {f"{bundle}/{name}.json" for bundle in (
        "agent-queue-parent-integration", "agent-queue-root-train", "parent-integration", "root-train",
    ) for name in ("artifact", "diagnostics")}
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/" + path for path in active | retired
    }
    for path, evidence in changes["playbook_fixtures"].items():
        suffix = path.removeprefix("tests/fixtures/playbooks/v2/")
        expected = path.replace("/v2/", "/historical-v2/") if suffix in retired else path
        assert evidence["path"] == expected
    return changes


def _dev_changes():
    changes = json.loads(DEV_CHANGES.read_text())
    assert changes["source_base"] == "b17f5d8adb9bc2c15ddeaef8260812f281ee055b"
    assert set(changes["contracts"]) == {"integration_redrive_child"}
    assert set(changes["transport_definitions"]) == {"integration_redrive_child"}
    return changes


def _hibernation_changes():
    changes = json.loads(HIBERNATION_CHANGES.read_text())
    assert changes["source_base"] == "4385c73158425a08114164893b623cf6b65718e8"
    assert set(changes["contracts"]) == {
        "job_cancel", "job_get", "job_submit", "message_wait",
        "wait_cancel", "wait_get", "wait_list", "wait_register",
    }
    assert set(changes["transport_definitions"]) == {
        "edit_task", "wait_cancel", "wait_get", "wait_list", "wait_register",
    }
    return changes


def _supervisor_health_changes():
    changes = json.loads(SUPERVISOR_HEALTH_CHANGES.read_text())
    assert changes["source_base"] == "a73e55ae16605561e6ce371bab9685455cc1f34e"
    assert set(changes["transport_definitions"]) == {"supervisor_restart"}
    return changes


def _structure_changes():
    changes = json.loads(STRUCTURE_CHANGES.read_text())
    assert changes["source_base"] == "908112dce62681fb4761e80a218382c75fc939cf"
    assert changes["source_commit"] == "f136e4af2c8f05a3d52cce6118fab78e03733a9e"
    assert set(changes["added_commands"]) == {"task_split"}
    assert set(changes["transport_definitions"]) == {"reparent_task"}
    assert set(changes["contracts"]) == {"integration_mutate_hierarchy"}
    return changes


def _revert_structure_catalogue(catalogue):
    changes = _structure_changes()
    for name, approved in changes["added_commands"].items():
        assert catalogue["fallback"].pop(name) == approved["transport_definition"]
        assert catalogue["tool_categories"].pop(name) == approved["category"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    for name, approved in changes["transport_definitions"].items():
        [definition] = [row for row in catalogue["explicit"] if row["name"] == name]
        assert definition == approved["current"]
        schema = json.loads(json.dumps(definition["input_schema"]))
        for field in ("expected_old_generation", "expected_new_generation"):
            assert schema["properties"].pop(field)["type"] == "integer"
            assert field not in schema.get("required", ())
        assert schema == approved["previous"]["input_schema"]
        definition.clear()
        definition.update(approved["previous"])
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        changes["previous_cli_catalogue_sha256"]
    )
    return catalogue


def _revert_target_repair_catalogue(catalogue):
    """Remove the new repair command and recover the preceding catalogue bytes."""
    catalogue = _revert_structure_catalogue(catalogue)
    repair = _target_repair_changes()
    for name, approved in repair["added_commands"].items():
        assert catalogue["fallback"].pop(name) == approved["transport_definition"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        repair["previous_cli_catalogue_sha256"]
    )
    return catalogue


def _revert_supervisor_health_catalogue(catalogue):
    """Restore only the reviewed description text to recover the hibernation bytes."""
    catalogue = _revert_target_repair_catalogue(catalogue)
    changes = _supervisor_health_changes()
    for name, approved in changes["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        assert definition["description"] == approved["description"]
        definition["description"] = approved["previous_description"]
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


def _target_repair_changes():
    changes = json.loads(TARGET_REPAIR_CHANGES.read_text())
    assert changes["source_commit"] == "8ec0c84e3afdd0b9bc79b9dbd7b59df197a47ff6"
    assert set(changes["added_commands"]) == {"integration_repair_target"}
    return changes


def _publication_changes():
    changes = json.loads(PUBLICATION_CHANGES.read_text())
    assert changes["source_base"] == "f9a7591766d69481b309e8733be795e62ab0e59e"
    assert set(changes["transport_definitions"]) == {"git_push", "push_branch"}
    assert set(changes["added_commands"]) == {
        "integration_delegate_publication", "integration_revoke_publication",
    }
    return changes


def _revert_hibernation_catalogue(catalogue):
    delta = _hibernation_changes()["catalogue_delta"]
    previous = delta["previous_explicit"]
    catalogue["explicit"] = [
        previous.get(entry["name"], entry) for entry in catalogue["explicit"]
        if entry["name"] not in delta["added_explicit"]
    ]
    for key in ("fallback", "tool_categories"):
        for name in delta[f"added_{key}"]:
            catalogue[key].pop(name)
    return catalogue


def _withheld_catalogue():
    """The publication catalogue minus exactly the reviewed delegation delta."""
    changes = _publication_changes()
    catalogue = _publication_catalogue()
    for name, added in changes["added_commands"].items():
        assert catalogue["fallback"].pop(name) == added["transport_definition"]
        assert CONTRACTS.require(name).contract.fingerprint() == added["contract_fingerprint"]
    [(prop, schema)] = changes["added_property"].items()
    seen = set()
    for _plugin, definitions in catalogue["internal"]:
        for definition in definitions:
            if definition["name"] in changes["transport_definitions"]:
                assert definition["input_schema"]["properties"].pop(prop) == schema
                seen.add(definition["name"])
    assert seen == set(changes["transport_definitions"])
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


def _batch_repair_changes():
    changes = json.loads(BATCH_REPAIR_CHANGES.read_text())
    assert changes["source_base"] == "ee36726bc1ae824a79787c2805c998422197da2a"
    assert changes["source_commit"] == "67575c6b9a43f820d824ff2bdb7d6be41ec21db3"
    assert set(changes["transport_definitions"]) == {"task_set"}
    assert set(changes["added_property"]) == {"repair_batch"}
    return changes


def _publication_catalogue():
    """Remove only the optional batch assignment to recover prior catalogue bytes."""
    changes = _batch_repair_changes()
    catalogue = _revert_hibernation_catalogue(
        _revert_supervisor_health_catalogue(json.loads(render_command_catalogue()))
    )
    [(prop, schema)] = changes["added_property"].items()
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "task_set"]
    assert definition["input_schema"]["properties"].pop(prop) == schema
    assert prop not in definition["input_schema"].get("required", ())
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


def _watch_changes():
    changes = json.loads(WATCH_CHANGES.read_text())
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch/diagnostics.json",
    }
    reminder = "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/"
    assert set(changes["new_playbook_fixtures"]) == {
        reminder + name for name in (
            "artifact.json", "artifact.sha256", "diagnostics.json", "manifest.md", "source.md",
        )
    }
    return changes


def _snapshot(*, handlers=(), explicit=(), fallback=None, typed=None, plugins=()):
    return build_descriptor_snapshot(
        handlers=handlers, explicit_definitions=explicit, fallback_schemas=fallback or {},
        typed_contracts=typed or {}, plugins=plugins,
    )


def test_legacy_execution_documents_and_fingerprints():
    fixture = json.loads(GOLDEN.read_text())
    changes = {**_observation_changes()["contracts"], **_script_changes()["contracts"],
               **_main_changes()["contracts"], **_dev_changes()["contracts"]}
    hibernation = _hibernation_changes()["contracts"]
    for name, old in fixture["legacy_contracts"].items():
        expected = changes.get(name, old)
        if name in changes:
            assert expected["previous_fingerprint"] == old["fingerprint"]
            assert expected["fingerprint"] != old["fingerprint"]
        if name in hibernation:
            assert hibernation[name]["previous_fingerprint"] == expected["fingerprint"]
            assert hibernation[name]["fingerprint"] != expected["fingerprint"]
            expected = hibernation[name]
        structure = _structure_changes()["contracts"]
        if name in structure:
            assert structure[name]["previous_fingerprint"] == expected["fingerprint"]
            assert structure[name]["fingerprint"] != expected["fingerprint"]
            expected = structure[name]
        contract = CONTRACTS.require(name).contract
        assert canonical_execution_document(contract.execution) == expected["execution"], name
        assert contract.fingerprint() == expected["fingerprint"], name
    assert set(fixture["legacy_contracts"]) - command_snapshot().handler_names == {
        "count_project_memory_files", "read_project_memory_file", "git_diff",
        "memory_save", "memory_search",
    }
    descriptors = command_snapshot().commands
    for name in ("count_project_memory_files", "read_project_memory_file"):
        assert descriptors[name].origin == "helper" and descriptors[name].available
    for name in ("git_diff", "memory_save", "memory_search"):
        assert descriptors[name].origin == "plugin" and descriptors[name].plugin


def test_transport_views_and_cli_serialization_preserve_golden_evidence():
    fixture = json.loads(GOLDEN.read_text())
    changes = _observation_changes()
    scripts = _script_changes()
    main = _main_changes()
    dev = _dev_changes()
    hibernation = _hibernation_changes()
    publication = _publication_changes()
    batch_repair = _batch_repair_changes()
    supervisor_health = _supervisor_health_changes()
    contracts = {**changes["contracts"], **scripts["contracts"]}
    snapshot = command_snapshot()
    for name, digest in fixture["transport_definitions"].items():
        if name in contracts:
            approved = contracts[name]
            assert approved["previous_transport_sha256"] == digest
            digest = approved["transport_sha256"]
        if name in main["transport_definitions"]:
            approved = main["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in dev["transport_definitions"]:
            approved = dev["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in hibernation["transport_definitions"]:
            approved = hibernation["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in publication["transport_definitions"]:
            approved = publication["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
            assert CONTRACTS.require(name).contract.fingerprint() == approved["fingerprint"]
        if name in batch_repair["transport_definitions"]:
            approved = batch_repair["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in supervisor_health["transport_definitions"]:
            approved = supervisor_health["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in _structure_changes()["transport_definitions"]:
            approved = _structure_changes()["transport_definitions"][name]
            assert _digest(approved["previous"]) == digest
            digest = _digest(approved["current"])
        assert _digest(snapshot.commands[name].tool_definition()) == digest, name
    actual = hashlib.sha256(render_command_catalogue().encode()).hexdigest()
    assert changes["previous_cli_catalogue_sha256"] == fixture["cli_catalogue_sha256"]
    assert scripts["previous_cli_catalogue_sha256"] == changes["cli_catalogue_sha256"]
    assert main["previous_cli_catalogue_sha256"] == scripts["cli_catalogue_sha256"]
    assert dev["previous_cli_catalogue_sha256"] == main["cli_catalogue_sha256"]
    withheld = json.loads(WITHHELD_CHANGES.read_text())
    assert withheld["previous_cli_catalogue_sha256"] == dev["cli_catalogue_sha256"]
    assert publication["previous_cli_catalogue_sha256"] == withheld["cli_catalogue_sha256"]
    assert batch_repair["previous_cli_catalogue_sha256"] == publication["cli_catalogue_sha256"]
    assert hibernation["previous_cli_catalogue_sha256"] == batch_repair["cli_catalogue_sha256"]
    assert supervisor_health["previous_cli_catalogue_sha256"] == hibernation["cli_catalogue_sha256"]
    repair = _target_repair_changes()
    assert repair["previous_cli_catalogue_sha256"] == supervisor_health["cli_catalogue_sha256"]
    structure = _structure_changes()
    assert structure["previous_cli_catalogue_sha256"] == repair["cli_catalogue_sha256"]
    assert actual == structure["cli_catalogue_sha256"]
    definitions = {d["name"]: d for d in effective_tool_definitions()}
    for name, definition in _discover_all_commands().items():
        assert definition == snapshot.commands[name].tool_definition()
        if name in definitions:
            assert definition == definitions[name]


def test_withheld_source_catalogue_only_adds_reviewed_command():
    evidence = json.loads(WITHHELD_CHANGES.read_text())
    assert evidence["source_base"] == "35d58eee0183bcc25252931ae5958072983606d6"
    name = "integration_release_withheld_source"
    catalogue = _withheld_catalogue()
    assert catalogue["fallback"].pop(name) == evidence["transport_definition"]
    assert CONTRACTS.require(name).contract.fingerprint() == evidence["contract_fingerprint"]
    # Removing exactly the new command must recover the prior bytes, including
    # schema field ordering. Existing commands and category maps stay pinned.
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == _dev_changes()["cli_catalogue_sha256"]


def test_publication_delegation_catalogue_only_adds_reviewed_commands_and_grant():
    changes = _publication_changes()
    _withheld_catalogue()
    snapshot = command_snapshot()
    [(prop, schema)] = changes["added_property"].items()
    for name, approved in changes["transport_definitions"].items():
        definition = snapshot.commands[name].tool_definition()
        assert definition["input_schema"]["properties"][prop] == schema
        assert prop not in definition["input_schema"].get("required", ())
        assert CONTRACTS.require(name).contract.fingerprint() != approved["previous_fingerprint"]


def test_batch_repair_catalogue_only_adds_optional_assignment():
    _publication_catalogue()
    schema = command_snapshot().commands["task_set"].tool_definition()["input_schema"]
    assert schema["properties"]["repair_batch"]["type"] == "string"
    assert "repair_batch" not in schema.get("required", ())


def test_target_repair_catalogue_only_adds_reviewed_command():
    _revert_target_repair_catalogue(json.loads(render_command_catalogue()))


def test_dev_redrive_contract_only_adds_explicit_source_adoption():
    old = json.loads(GOLDEN.read_text())["legacy_contracts"]["integration_redrive_child"][
        "execution"
    ]
    old["args_schema"]["properties"]["adopt_source"] = {
        "default": False, "type": "boolean",
    }
    old["result_schema"]["properties"]["completion_id"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}], "default": None,
    }
    old["receipt_projection"].insert(old["receipt_projection"].index("collection"), "completion_id")
    assert _dev_changes()["contracts"]["integration_redrive_child"]["execution"] == old


def test_legacy_playbook_fixtures_are_unchanged():
    changes = _observation_changes()["playbook_fixtures"]
    assert set(changes) == {
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json",
    }
    changes = {**changes, **_script_changes()["playbook_fixtures"]}
    main = _main_changes()["playbook_fixtures"]
    watch = _watch_changes()["playbook_fixtures"]
    ingest = json.loads(INGEST_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    for path, digest in json.loads(GOLDEN.read_text())["playbook_fixtures"].items():
        if path in changes:
            assert changes[path]["previous_sha256"] == digest
            digest = changes[path]["sha256"]
        if path in main:
            assert main[path]["previous_sha256"] == digest
            digest = main[path]["sha256"]
            path = main[path]["path"]
        if path in watch:
            assert watch[path]["previous_sha256"] == digest
            historical = ROOT / watch[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert watch[path]["sha256"] != digest
            digest = watch[path]["sha256"]
        if path in ingest:
            assert ingest[path]["previous_sha256"] == digest
            assert ingest[path]["sha256"] != digest
            digest = ingest[path]["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_watch_reminder_additions_match_explicit_pins():
    for path, digest in _watch_changes()["new_playbook_fixtures"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_script_log_contract_only_adds_stream_selection():
    old = json.loads(GOLDEN.read_text())["legacy_contracts"]["job_logs"]["execution"]
    old["args_schema"]["properties"]["stream"] = {
        "default": "stdout", "enum": ["stdout", "stderr"], "type": "string",
    }
    assert _script_changes()["contracts"]["job_logs"]["execution"] == old


def test_inventory_coverage_is_derived_with_exact_denials():
    snapshot = command_snapshot()
    discovered = {h.name for h in discover_handler_inventory(CommandHandler)}
    assert snapshot.handler_names == discovered
    assert discovered - PLAYBOOK_DENIALS.keys() <= CONTRACTS.names()
    assert not (PLAYBOOK_DENIALS.keys() & CONTRACTS.names())
    assert {n for n, d in snapshot.commands.items() if d.denial_reason} == set(PLAYBOOK_DENIALS)
    report = {r["name"]: r for r in snapshot.schema_quality_report()}
    assert report.keys() == snapshot.commands.keys()
    assert any(r["quality"] == "opaque" for r in report.values())


def test_handler_provider_installation_remains_late_bound():
    calls = []

    def provider():
        calls.append(True)
        return SimpleNamespace()

    set_handler_provider(provider)
    try:
        assert calls == []
        first = command_snapshot()
        assert calls
        assert command_snapshot() is first
    finally:
        set_handler_provider(None)


def test_generated_inventory_does_not_suppress_legacy_scope_injection():
    from src.api.scope import _forbids_project_id

    generated = CONTRACTS.names() - CONTRACTS.typed_contracts().keys()
    assert generated
    assert not {name for name in generated if _forbids_project_id(name)}


class NewHandler:
    async def _cmd_newly_discovered(self, args):
        return {"success": True, "forwarded": args}

    async def _cmd_argumentless(self, args):
        return {"success": True}


def test_new_commands_need_no_registration_and_unknown_source_is_opaque():
    snapshot = _snapshot(handlers=discover_handler_inventory(NewHandler))
    registry = ContractRegistry()
    registry.replace_descriptors(snapshot)
    assert registry.names() == {"newly_discovered", "argumentless"}
    opaque = snapshot.commands["newly_discovered"]
    assert opaque.schema_quality == "opaque"
    assert opaque.input_schema()["additionalProperties"] is True
    assert opaque.effect_class == "destructive" and opaque.sensitivity == "protected"
    assert not opaque.retry_safe and not opaque.preview_available
    assert opaque.idempotency_mechanism == "none"
    assert snapshot.commands["argumentless"].input_schema()["additionalProperties"] is False
    assert argument_usage(len) is None
    native = _snapshot(handlers=[HandlerCommand("native", "Native", None)]).commands["native"]
    assert native.schema_source == "opaque"


def test_typed_playbook_schema_wins_and_explicit_transport_discrepancy_is_audited():
    contract = CONTRACTS.require("message_send").contract
    schema = {"type": "object", "properties": {"legacy": {"type": "string"}}}
    snapshot = _snapshot(
        handlers=[HandlerCommand("message_send", "Send", True)],
        explicit=[{"name": "message_send", "description": "Legacy", "input_schema": schema}],
        fallback={"message_send": {"type": "object", "additionalProperties": False}},
        typed={"message_send": contract},
    )
    descriptor = snapshot.commands["message_send"]
    assert descriptor.input_schema() == contract.execution.args_model.model_json_schema()
    assert descriptor.input_schema("transport") == schema
    assert descriptor.schema_source == "typed_contract"
    assert descriptor.transport_schema_source == "explicit_tool"
    assert descriptor.audit == ("explicit_transport_schema_differs_from_typed_playbook_schema",)
    assert descriptor.result_mode == "typed"
    assert descriptor.idempotency_mechanism == contract.execution.idempotency.mode
    with pytest.raises(ValueError, match="schema view"):
        descriptor.input_schema("unknown")


@pytest.mark.parametrize("source", ["explicit_tool", "fallback"])
def test_declared_schema_precedes_argument_usage(source):
    schema = {"type": "object", "properties": {"count": {"type": "integer", "default": 3}}}
    snapshot = _snapshot(
        handlers=[HandlerCommand("read", "Read", False)],
        explicit=[{"name": "read", "input_schema": schema}] if source == "explicit_tool" else [],
        fallback={"read": schema},
    )
    descriptor = snapshot.commands["read"]
    assert descriptor.input_schema() == schema and descriptor.schema_source == source


def test_argument_usage_does_not_depend_on_the_parameter_spelling():
    async def renamed(self, payload):
        return payload

    assert argument_usage(renamed) is True


def test_snapshot_and_nested_schemas_cannot_be_mutated_by_consumers():
    schema = {"type": "object", "properties": {"key": {"enum": ["one", "two"]}}}
    descriptor = _snapshot(
        handlers=[HandlerCommand("read", "Read", True)], fallback={"read": schema},
    ).commands["read"]
    schema["properties"].clear()
    descriptor.input_schema()["properties"].clear()
    descriptor.tool_definition()["input_schema"].clear()
    assert descriptor.input_schema()["properties"]["key"]["enum"] == ["one", "two"]
    with pytest.raises(FrozenInstanceError):
        descriptor.available = False
    with pytest.raises(TypeError):
        command_snapshot().commands["invented"] = descriptor


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "$defs": {"choice": {"type": "string", "enum": ["yes", "no"]}},
    "properties": {
        "count": {"type": "integer", "minimum": 1, "default": 2},
        "choice": {"$ref": "#/$defs/choice"},
        "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "nested": {"type": "object", "properties": {"flag": {"type": "boolean"}},
                   "required": ["flag"], "additionalProperties": False},
    },
    "required": ["count", "choice"],
}


@pytest.fixture
def json_args():
    descriptor = _snapshot(handlers=[HandlerCommand("generic", "Generic", True)],
                           fallback={"generic": SCHEMA}).commands["generic"]
    return argument_model(descriptor)


def test_json_models_preserve_defs_defaults_nullability_and_unwrapped_payload(json_args):
    assert json_args.model_json_schema() == SCHEMA
    raw = {"count": 2, "choice": "yes", "nullable": None, "nested": {"flag": False}}
    assert json_args.model_validate(raw).model_dump() == raw
    minimal = {"count": 2, "choice": "no"}
    assert json_args.model_validate(minimal).model_dump() == minimal
    payload = {"root": {"payload": [1, None, False]}, "success": True}
    assert JsonObjectResult.model_validate(payload).model_dump() == payload


@pytest.mark.parametrize("payload", [
    {"count": "2", "choice": "yes"}, {"count": True, "choice": "yes"},
    {"count": 0, "choice": "yes"}, {"count": 2, "choice": "maybe"},
    {"count": 2}, {"count": 2, "choice": "yes", "extra": 1},
    {"count": 2, "choice": "yes", "nested": {"flag": "false"}},
    {"count": 2, "choice": "yes", "nullable": 1},
    {"count": float("nan"), "choice": "yes"},
])
def test_strict_schema_validation_precedes_coercion(json_args, payload):
    with pytest.raises(ValidationError):
        json_args.model_validate(payload)


@pytest.mark.parametrize("payload", [{1: "value"}, {"value": (1, 2)},
                                      {"value": float("inf")}, []])
def test_result_model_rejects_non_json_values(payload):
    with pytest.raises(ValidationError):
        JsonObjectResult.model_validate(payload)


def test_opaque_arguments_forward_additional_properties_without_wrapping():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    raw = {"root": 3, "unknown": {"nested": [True, None, 2.5]}}
    assert argument_model(descriptor).model_validate(raw).model_dump() == raw


def test_remote_schema_references_cannot_fetch_resources(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network access"))
    descriptor = _snapshot(
        handlers=[HandlerCommand("remote", "Remote", True)],
        fallback={"remote": {"type": "object", "properties": {
            "data": {"$ref": "https://example.invalid/schema"},
        }}},
    ).commands["remote"]
    model = argument_model(descriptor)
    with pytest.raises(ValidationError, match="local"):
        model.model_validate({"data": {}})


async def test_generated_invocation_is_disabled_including_unavailable_and_denied():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    registration = generated_registration(descriptor)
    args = registration.contract.execution.args_model.model_validate({})
    with pytest.raises(GeneratedInvocationDisabled):
        await registration.invoke(args, None)
    for current, code in [(replace(descriptor, available=False), "command_unavailable"),
                          (replace(descriptor, denial_reason="forbidden"), "playbook_command_denied")]:
        registration = generated_registration(current)
        args = registration.contract.execution.args_model.model_validate({})
        assert (await registration.invoke(args, None)) == {"success": False, "error_code": code}


def test_generated_fingerprint_is_generation_two_and_ignores_inventory_churn():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    first = generated_registration(descriptor).contract
    assert canonical_execution_document(first.execution)["schema_generation"] == 2
    unavailable = generated_registration(replace(descriptor, available=False)).contract
    assert first.fingerprint() == unavailable.fingerprint()
    changed = generated_registration(replace(descriptor, sensitivity="audited_public")).contract
    assert changed.fingerprint() != first.fingerprint()


def test_generated_fingerprint_preserves_schema_keys_and_literal_data():
    def contract(value):
        schema = {"type": "object", "properties": {
            "description": {"const": {"title": value, "required": [2, "one"]}},
        }}
        descriptor = _snapshot(handlers=[HandlerCommand("literal", "Literal", True)],
                               fallback={"literal": schema}).commands["literal"]
        return generated_registration(descriptor).contract, schema

    first, schema = contract("first")
    second, _ = contract("second")
    assert canonical_execution_document(first.execution)["args_schema"] == schema
    assert first.fingerprint() != second.fingerprint()


async def test_plugin_load_unload_refreshes_live_snapshot_and_retains_unavailable(tmp_path, monkeypatch):
    class ProbePlugin(InternalPlugin):
        async def initialize(self, ctx):
            ctx.register_command("probe", self.probe)
            ctx.register_tool({"name": "probe", "description": "Probe", "input_schema": SCHEMA})

        async def probe(self, args):
            return {"success": True}

        async def shutdown(self, ctx):
            pass

    monkeypatch.setattr("src.plugins.internal.discover_internal_plugins",
                        lambda: [("src.plugins.internal.probe", ProbePlugin)])
    config = AppConfig(data_dir=str(tmp_path))
    plugins = PluginRegistry(db=AsyncMock(), bus=AsyncMock(), config=config)
    handler = CommandHandler(SimpleNamespace(plugin_registry=plugins), config)
    set_handler_provider(lambda: handler)
    try:
        before = command_snapshot()
        assert "probe" not in before.commands
        assert await plugins.load_internal_plugins() == 1
        loaded = command_snapshot()
        assert loaded is handler.command_descriptors
        assert loaded is not before and loaded.plugin_generation > before.plugin_generation
        assert loaded.commands["probe"].available and loaded.commands["probe"].plugin == "aq-probe"
        assert {"probe", "aq-probe.probe"} <= CONTRACTS.names()
        assert CONTRACTS.require("probe").contract.execution.args_model.model_json_schema() == SCHEMA
        await plugins.unload_plugin("aq-probe")
        unloaded = command_snapshot()
        assert unloaded.plugin_generation > loaded.plugin_generation
        assert not unloaded.commands["probe"].available and loaded.commands["probe"].available
        registration = CONTRACTS.require("probe")
        args = registration.contract.execution.args_model.model_validate({"count": 1, "choice": "yes"})
        assert await registration.invoke(args, None) == {
            "success": False, "error_code": "command_unavailable",
        }
    finally:
        set_handler_provider(None)


def test_declared_plugin_is_visible_without_fabricating_availability():
    plugin = PluginCommand.from_definition({"name": "optional", "input_schema": SCHEMA},
                                           plugin="third-party", available=False)
    descriptor = _snapshot(plugins=[plugin]).commands["optional"]
    assert descriptor.origin == "plugin" and not descriptor.available
    assert descriptor.schema_source == "explicit_tool"


def test_unresolvable_local_reference_and_invalid_schema_are_isolated():
    for schema in [
        {"type": "object", "properties": {"value": {"$ref": "#/$defs/missing"}}},
        {"type": "object", "properties": {"value": {"type": "not-a-json-type"}}},
    ]:
        snapshot = _snapshot(handlers=[HandlerCommand("invalid", "Invalid", True)],
                             fallback={"invalid": schema})
        registry = ContractRegistry()
        registry.replace_descriptors(snapshot)
        model = registry.require("invalid").contract.execution.args_model
        assert model.model_json_schema() == schema
        with pytest.raises(ValidationError):
            model.model_validate({"value": 1})


def test_reference_looking_literal_data_is_not_a_schema_reference():
    literal = {"$ref": "https://example.invalid/literal-not-a-reference"}
    schema = {"type": "object", "properties": {"data": {"const": literal, "default": literal}}}
    descriptor = _snapshot(handlers=[HandlerCommand("literal", "Literal", True)],
                           fallback={"literal": schema}).commands["literal"]
    assert argument_model(descriptor).model_validate({"data": literal}).model_dump() == {"data": literal}


def test_generic_argument_lookup_and_executor_never_invent_a_root_argument():
    from src.playbooks.executors.command import _build_args
    from src.playbooks.explanation import canonical_effect
    from src.playbooks.validation import RegistryContractLookup

    registry = ContractRegistry()
    registry.replace_descriptors(_snapshot(handlers=discover_handler_inventory(NewHandler)))
    registration = registry.require("newly_discovered")
    info = RegistryContractLookup(registry).get("newly_discovered")
    assert not info.arguments
    assert info.argument_schema["additionalProperties"] is True
    raw = {"root": {"nested": 5}, "another": 1}
    args, forwarded, error = _build_args(registration, SimpleNamespace(inputs=raw), key=None)
    assert error is None and forwarded == raw and args.model_dump() == raw
    assert canonical_effect(registration.contract).text == "Composite using a JSON object"


def test_missing_dynamic_path_fails_before_generated_invocation(json_args):
    from src.playbooks.expressions import (
        BindingRef,
        ResolutionScope,
        ValueResolutionError,
        resolve_value,
    )

    # The expression layer's missing marker cannot be coerced into JSON or
    # silently supplied as a default by the generated argument model.
    with pytest.raises(ValueResolutionError):
        value = resolve_value(BindingRef(binding="result", path="missing"),
                              ResolutionScope(bindings={"result": {"known": 1}}))
        json_args.model_validate({"count": value, "choice": "yes"})
