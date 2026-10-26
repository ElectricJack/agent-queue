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
EPIC_CHANGES = ROOT / "tests/fixtures/contracts/epic-integration-contract-changes.json"
EPIC_COMMANDS = ("integration_cutover_containment", "integration_reconcile_git_truth")
WATCH_CHANGES = ROOT / "tests/fixtures/contracts/watch-contract-changes.json"
INGEST_CHANGES = ROOT / "tests/fixtures/contracts/ingestion-replay-contract-changes.json"
OBJECT_LOOP_PORT_CHANGES = ROOT / "tests/fixtures/contracts/object-loop-port-changes.json"
PIPELINE_CHANGES = ROOT / "tests/fixtures/contracts/default-pipeline-building-blocks-changes.json"
HIBERNATION_CHANGES = ROOT / "tests/fixtures/contracts/hibernation-contract-changes.json"
DISPATCH_CHANGES = ROOT / "tests/fixtures/contracts/dispatch-wait-contract-changes.json"
BATCH_FIX_CHANGES = ROOT / "tests/fixtures/contracts/batch-fix-contract-changes.json"
MERGE_CHANGES = ROOT / "tests/fixtures/contracts/epic-main-refresh-contract-changes.json"
LEAF_CHANGES = ROOT / "tests/fixtures/contracts/batch745-leaf-contract-changes.json"
COMPLETION_CHANGES = ROOT / "tests/fixtures/contracts/fast-smooth-completion-contract-changes.json"
PUBLICATION_CHANGES = ROOT / "tests/fixtures/contracts/publication-delegation-contract-changes.json"
BATCH_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/batch-repair-contract-changes.json"
DEV_HIBERNATION_CHANGES = ROOT / "tests/fixtures/contracts/hibernation-main-refresh-contract-changes.json"
SUPERVISOR_HEALTH_CHANGES = ROOT / "tests/fixtures/contracts/supervisor-health-contract-changes.json"
TARGET_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/target-repair-contract-changes.json"
OBJECT_LOOP_CHANGES = ROOT / "tests/fixtures/contracts/object-loop-refactor-contract-changes.json"
PR_REPAIR_CHANGES = ROOT / "tests/fixtures/contracts/pr-repair-publication-contract-changes.json"
PROMOTION_REQUEST_PORT = ROOT / "tests/fixtures/contracts/promotion-request-port-changes.json"
FAILOVER_CHANGES = ROOT / "tests/fixtures/contracts/provider-failover-playbook-changes.json"
TRIAGE_CHANGES = ROOT / "tests/fixtures/contracts/supervisor-triage-artifact-changes.json"
MORNING_CHANGES = ROOT / "tests/fixtures/contracts/morning-report-building-block-changes.json"
WAIT_REQUEUE_CHANGES = ROOT / "tests/fixtures/contracts/wait-requeue-contract-changes.json"
RESTART_HOLD_CHANGES = ROOT / "tests/fixtures/contracts/restart-hold-contract-changes.json"
ROUTING_CHANGES = ROOT / "tests/fixtures/contracts/routing-building-blocks-artifact-changes.json"


STRUCTURE_CHANGES = ROOT / "tests/fixtures/contracts/post-release-structure-contract-changes.json"


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
    return catalogue


def test_structure_catalogue_preserves_reviewed_delta():
    changes = _structure_changes()
    for name, approved in changes["contracts"].items():
        contract = CONTRACTS.require(name).contract
        assert contract.fingerprint() == approved["fingerprint"]
        assert canonical_execution_document(contract.execution) == approved["execution"]
    previous = _revert_catalogue(
        _revert_structure_catalogue(_before_restart_hold_catalogue()),
        _wait_requeue_changes(),
    )
    assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
        _pr_repair_changes()["cli_catalogue_sha256"]
    )


UPDATER_CHANGES = ROOT / "tests/fixtures/contracts/updater-safety-contract-changes.json"


def _updater_changes():
    return json.loads(UPDATER_CHANGES.read_text())


def _before_updater_catalogue(catalogue=None):
    catalogue = _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue
    for name, approved in _updater_changes()["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        assert definition["description"] == approved["description"]
        assert _digest(definition) == approved["sha256"]
        definition["description"] = approved["previous_description"]
        assert _digest(definition) == approved["previous_sha256"]
    return catalogue


def test_updater_catalogue_only_changes_guarded_update_description():
    evidence = _updater_changes()
    assert set(evidence["transport_definitions"]) == {"update_and_restart"}
    catalogue = _before_wait_requeue_catalogue()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    assert evidence["previous_cli_catalogue_sha256"] == _pr_repair_changes()["cli_catalogue_sha256"]
    for name, approved in evidence["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        definition["description"] = approved["description"]
        assert _digest(definition) == approved["sha256"]
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]


def _stuck_check_changes():
    return json.loads((ROOT / "tests/fixtures/contracts/stuck-check-contract-changes.json").read_text())


def _pr_repair_changes():
    return json.loads(PR_REPAIR_CHANGES.read_text())


def _restart_hold_changes():
    return json.loads(RESTART_HOLD_CHANGES.read_text())


def _before_restart_hold_catalogue():
    catalogue = _before_parallel_catalogue()
    approved = _restart_hold_changes()["transport_definitions"]["restart_task"]
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "restart_task"]
    assert definition == approved["definition"]
    definition.clear()
    definition.update(approved["previous_definition"])
    return catalogue


def test_restart_hold_catalogue_preserves_original_pr_evidence():
    # Recover the PR's supervisor-health base through dev's frozen input chain.
    catalogue = _revert_target_repair_catalogue(_dev_catalogue())
    evidence = _restart_hold_changes()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    approved = evidence["transport_definitions"]["restart_task"]
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "restart_task"]
    assert definition == approved["previous_definition"]
    definition.clear()
    definition.update(approved["definition"])
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]


def _wait_requeue_changes():
    return json.loads(WAIT_REQUEUE_CHANGES.read_text())


def _before_wait_requeue_catalogue():
    return _revert_catalogue(
        _revert_structure_catalogue(_before_restart_hold_catalogue()),
        _wait_requeue_changes(),
    )


def test_wait_requeue_catalogue_only_changes_reviewed_command():
    evidence = _wait_requeue_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    assert {name for section in evidence["catalogue_sections"].values()
            for name in section["entries"]} == {evidence["command"]}
    previous = json.dumps(_before_wait_requeue_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )
    assert evidence["previous_cli_catalogue_sha256"] == _pr_repair_changes()[
        "cli_catalogue_sha256"
    ]


def _before_pr_repair_catalogue():
    evidence = _pr_repair_changes()
    catalogue = _before_wait_requeue_catalogue()
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


def test_pr_repair_catalogue_only_adds_reviewed_command():
    evidence = _pr_repair_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    previous = json.dumps(_before_pr_repair_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


def _before_stuck_check_catalogue():
    evidence = _stuck_check_changes()
    catalogue = _before_pr_repair_catalogue()
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


def test_stuck_check_catalogue_only_adds_reviewed_command():
    evidence = _stuck_check_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    previous = json.dumps(_before_stuck_check_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


def _completion_changes():
    return json.loads(COMPLETION_CHANGES.read_text())


def _before_completion_catalogue():
    return _revert_catalogue(_before_stuck_check_catalogue(), _completion_changes())


def _dev_catalogue():
    return _revert_catalogue(_before_stuck_check_catalogue(), {
        "catalogue_sections": _completion_changes()["source_catalogue_sections"],
    })


def test_completion_catalogue_preserves_both_frozen_inputs():
    evidence = _completion_changes()
    for catalogue, expected in (
        (_before_completion_catalogue(), "previous_cli_catalogue_sha256"),
        (_dev_catalogue(), "source_cli_catalogue_sha256"),
    ):
        assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
            evidence[expected]
        )
    assert evidence["source_cli_catalogue_sha256"] == _target_repair_changes()[
        "cli_catalogue_sha256"
    ]



def _merge_changes():
    return json.loads(MERGE_CHANGES.read_text())


def _leaf_changes():
    return json.loads(LEAF_CHANGES.read_text())


def _before_leaf_catalogue():
    return _revert_catalogue(_before_completion_catalogue(), _leaf_changes())


def _before_merge_catalogue():
    return _revert_catalogue(_before_leaf_catalogue(), _merge_changes())


def test_leaf_catalogue_only_adds_reviewed_commands():
    evidence = _leaf_changes()
    assert evidence["source_base"] == "120f820ee85acd957525ac2b77c4bacaeed4c728"
    assert not evidence["contracts"] and not evidence["transport_definitions"]
    snapshot = command_snapshot()
    for name, approved in evidence["new_commands"].items():
        assert evidence["catalogue_sections"]["explicit"]["entries"][name]["previous"] is None
        assert _digest(snapshot.commands[name].tool_definition()) == (
            approved["transport_sha256"]), name
        assert CONTRACTS.require(name).contract.fingerprint() == (
            approved["contract_fingerprint"]), name
    # Removing exactly the leaf commands recovers the merged epic bytes.
    previous = json.dumps(_before_leaf_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence[
        "previous_cli_catalogue_sha256"] == _merge_changes()["cli_catalogue_sha256"]


def test_merged_catalogue_preserves_both_frozen_inputs():
    evidence = _merge_changes()
    for key, expected in (("catalogue_sections", "previous_cli_catalogue_sha256"),
                          ("source_catalogue_sections", "source_cli_catalogue_sha256")):
        previous = _revert_catalogue(_before_leaf_catalogue(),
                                    {"catalogue_sections": evidence[key]})
        assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
            evidence[expected])


def _hibernation_changes():
    return json.loads(HIBERNATION_CHANGES.read_text())


def _dispatch_changes():
    return json.loads(DISPATCH_CHANGES.read_text())


def _revert_catalogue(catalogue, changes):
    for section, evidence in changes["catalogue_sections"].items():
        current = catalogue[section]
        entries = (dict(current) if section == "internal" else
                   {row["name"]: row for row in current} if evidence["list"] else current)
        for name, change in evidence["entries"].items():
            assert entries.get(name) == change["current"], (section, name)
            if change["previous"] is None:
                entries.pop(name)
            else:
                entries[name] = change["previous"]
        previous = {name: entries[name] for name in evidence["previous_order"]}
        assert previous.keys() == entries.keys()
        catalogue[section] = (list(map(list, previous.items())) if section == "internal" else
                              list(previous.values()) if evidence["list"] else previous)
    return catalogue


def _before_dispatch_catalogue():
    return _revert_catalogue(_before_batch_fix_catalogue(), _dispatch_changes())


def _before_batch_fix_catalogue():
    evidence = json.loads(BATCH_FIX_CHANGES.read_text())
    catalogue = _before_merge_catalogue()
    name = "integration_batch_fix"
    definition = next(row for row in catalogue["explicit"] if row["name"] == name)
    assert definition == evidence["transport_definition"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(name) == evidence["tool_category"]
    return catalogue


def _before_hibernation_catalogue():
    return _revert_catalogue(_before_dispatch_catalogue(), _hibernation_changes())


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


def _epic_changes():
    changes = json.loads(EPIC_CHANGES.read_text())
    assert changes["source_base"] == "f88abe7fd245da853bd328187f01beda41e02e41"
    assert set(changes["transport_definitions"]) == set(EPIC_COMMANDS)
    assert set(changes["contract_fingerprints"]) == {"integration_cutover_containment"}
    return changes


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
    for name, old in fixture["legacy_contracts"].items():
        expected = changes.get(name, old)
        if name in changes:
            assert expected["previous_fingerprint"] == old["fingerprint"]
            assert expected["fingerprint"] != old["fingerprint"]
        for recording in (_hibernation_changes(), _dispatch_changes(), _merge_changes(),
                          _leaf_changes(), _completion_changes(), _structure_changes(),
                          _git_first_changes(), _obsolete_child_changes(),
                          _restart_hold_changes(), _updater_changes(), _task_list_limit_changes()):
            later = recording.get("contracts", {}).get(name)
            if later:
                assert later["previous_fingerprint"] == expected["fingerprint"]
                assert later["fingerprint"] != expected["fingerprint"]
                expected = later
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
        for recording in (
            _hibernation_changes(), _dispatch_changes(), _merge_changes(),
            _leaf_changes(), _completion_changes(), _restart_hold_changes(),
            _updater_changes(), _held_release_changes(), _workspace_changes(),
            _git_first_changes(), _obsolete_child_changes(), _task_list_limit_changes(),
        ):
            if name in recording["transport_definitions"]:
                approved = recording["transport_definitions"][name]
                assert approved["previous_sha256"] == digest
                digest = approved["sha256"]
        if name in _structure_changes()["transport_definitions"]:
            approved = _structure_changes()["transport_definitions"][name]
            assert _digest(approved["previous"]) == digest
            digest = _digest(approved["current"])
        if name == "task_batch_propose":
            settlement = _delivered_design_changes()
            assert _digest(settlement["previous_definition"]) == digest
            digest = _digest(settlement["definition"])
        if name in _unread_inbox_changes()["transport_definitions"]:
            approved = _unread_inbox_changes()["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        assert _digest(snapshot.commands[name].tool_definition()) == digest, name
    catalogue = _revert_structure_catalogue(_before_restart_hold_catalogue())
    actual = hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest()
    assert changes["previous_cli_catalogue_sha256"] == fixture["cli_catalogue_sha256"]
    assert scripts["previous_cli_catalogue_sha256"] == changes["cli_catalogue_sha256"]
    assert main["previous_cli_catalogue_sha256"] == scripts["cli_catalogue_sha256"]
    assert dev["previous_cli_catalogue_sha256"] == main["cli_catalogue_sha256"]
    withheld = json.loads(WITHHELD_CHANGES.read_text())
    assert withheld["previous_cli_catalogue_sha256"] == dev["cli_catalogue_sha256"]
    epic = _epic_changes()
    assert epic["previous_cli_catalogue_sha256"] == withheld["cli_catalogue_sha256"]
    hibernation = _hibernation_changes()
    assert hibernation["previous_cli_catalogue_sha256"] == epic["cli_catalogue_sha256"]
    dispatch = _dispatch_changes()
    assert dispatch["previous_cli_catalogue_sha256"] == hibernation["cli_catalogue_sha256"]
    batch_fix = json.loads(BATCH_FIX_CHANGES.read_text())
    assert batch_fix["previous_cli_catalogue_sha256"] == dispatch["cli_catalogue_sha256"]
    merge = _merge_changes()
    assert merge["previous_cli_catalogue_sha256"] == batch_fix["cli_catalogue_sha256"]
    leaf = _leaf_changes()
    assert leaf["previous_cli_catalogue_sha256"] == merge["cli_catalogue_sha256"]
    completion = _completion_changes()
    assert completion["previous_cli_catalogue_sha256"] == leaf["cli_catalogue_sha256"]
    stuck_check = _stuck_check_changes()
    assert stuck_check["previous_cli_catalogue_sha256"] == completion["cli_catalogue_sha256"]
    pr_repair = _pr_repair_changes()
    assert pr_repair["previous_cli_catalogue_sha256"] == stuck_check["cli_catalogue_sha256"]
    wait_requeue = _wait_requeue_changes()
    assert wait_requeue["previous_cli_catalogue_sha256"] == pr_repair["cli_catalogue_sha256"]
    assert actual == wait_requeue["cli_catalogue_sha256"]
    previous = json.dumps(_before_batch_fix_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == dispatch["cli_catalogue_sha256"]
    previous = json.dumps(_before_dispatch_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == hibernation["cli_catalogue_sha256"]
    previous = json.dumps(_before_hibernation_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == epic["cli_catalogue_sha256"]
    definitions = {d["name"]: d for d in effective_tool_definitions()}
    for name, definition in _discover_all_commands().items():
        assert definition == snapshot.commands[name].tool_definition()
        if name in definitions:
            assert definition == definitions[name]


def test_batch_fixer_catalogue_only_adds_scoped_publication_command():
    evidence = json.loads(BATCH_FIX_CHANGES.read_text())
    assert evidence["source_base"] == "d496e8a12c3e863aee09910856b018809f3eb21c"
    name = "integration_batch_fix"
    assert CONTRACTS.require(name).contract.fingerprint() == evidence["contract_fingerprint"]
    previous = json.dumps(_before_batch_fix_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence[
        "previous_cli_catalogue_sha256"
    ]


def test_withheld_source_catalogue_only_adds_reviewed_command():
    evidence = json.loads(WITHHELD_CHANGES.read_text())
    assert evidence["source_base"] == "35d58eee0183bcc25252931ae5958072983606d6"
    name = "integration_release_withheld_source"
    catalogue = _before_hibernation_catalogue()
    for later in EPIC_COMMANDS:
        catalogue["fallback"].pop(later)
    assert catalogue["fallback"].pop(name) == evidence["transport_definition"]
    assert CONTRACTS.require(name).contract.fingerprint() == evidence["contract_fingerprint"]
    # Removing exactly the new command must recover the prior bytes, including
    # schema field ordering. Existing commands and category maps stay pinned.
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == _dev_changes()["cli_catalogue_sha256"]


def test_epic_catalogue_only_adds_reviewed_commands():
    evidence = _epic_changes()
    catalogue = _before_hibernation_catalogue()
    for name in EPIC_COMMANDS:
        assert catalogue["fallback"].pop(name) == evidence["transport_definitions"][name], name
    for name, fingerprint in evidence["contract_fingerprints"].items():
        assert CONTRACTS.require(name).contract.fingerprint() == fingerprint, name
    # Removing exactly the epic's commands recovers the withheld-source bytes.
    previous = json.dumps(catalogue, indent=2) + "\n"
    withheld = json.loads(WITHHELD_CHANGES.read_text())
    assert hashlib.sha256(previous.encode()).hexdigest() == withheld["cli_catalogue_sha256"]


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
    continuous = json.loads(CONTINUOUS_CHANGES.read_text())["playbook_fixtures"]
    main = _main_changes()["playbook_fixtures"]
    watch = _watch_changes()["playbook_fixtures"]
    ingest = json.loads(INGEST_CHANGES.read_text())["playbook_fixtures"]
    hibernation = _hibernation_changes()["playbook_fixtures"]
    probe_port = json.loads((
        ROOT / "tests/fixtures/contracts/provider-usage-probe-port-changes.json"
    ).read_text())["playbook_fixtures"]
    assert set(probe_port) == {"tests/fixtures/playbooks/v2/provider-usage-probe/artifact.json"}
    object_loop_port = json.loads(OBJECT_LOOP_PORT_CHANGES.read_text())["playbook_fixtures"]
    assert set(object_loop_port) == {"tests/fixtures/playbooks/v2/object-loop/artifact.json"}
    object_loop = json.loads(OBJECT_LOOP_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    assert set(object_loop) == {
        "tests/fixtures/playbooks/v2/object-loop/artifact.json",
        "tests/fixtures/playbooks/v2/object-loop/traces.json",
    }
    promotion_request = json.loads(PROMOTION_REQUEST_PORT.read_text())["playbook_fixtures"]
    assert set(promotion_request) == {"tests/fixtures/playbooks/v2/promotion-request/artifact.json"}
    routing = json.loads(ROUTING_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    assert set(routing) == {"tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json"}
    held_pipeline = _held_ingestion_pipeline_transport_changes()["playbook_fixtures"]
    pipeline = json.loads(PIPELINE_CHANGES.read_text())["playbook_fixtures"]
    assert set(pipeline) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    wait_evidence = json.loads((
        ROOT / "tests/fixtures/contracts/default-pipeline-wait-grants-changes.json"
    ).read_text())
    wait_grants = wait_evidence["playbook_fixtures"]
    assert set(wait_grants) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    wait_change = wait_grants["tests/fixtures/playbooks/v2/default-pipeline/artifact.json"]
    assert wait_change["previous_sha256"] == pipeline[
        "tests/fixtures/playbooks/v2/default-pipeline/artifact.json"
    ]["sha256"]
    for name, digest in [
        (wait_change["preserved_previous_path"], wait_change["previous_sha256"]),
        ("tests/fixtures/playbooks/scenarios/default-pipeline/wait-grants.artifact.json",
         wait_change["sha256"]),
    ]:
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    composition = json.loads((
        ROOT / "tests/fixtures/contracts/default-pipeline-wait-grants-composition.json"
    ).read_text())["playbook_fixtures"]
    failover = json.loads(FAILOVER_CHANGES.read_text())["playbook_fixtures"]
    assert set(failover) == {"tests/fixtures/playbooks/v2/provider-failover/artifact.json"}
    triage = json.loads(TRIAGE_CHANGES.read_text())["playbook_fixtures"]
    assert set(triage) == {"tests/fixtures/playbooks/v2/supervisor-failure-triage/artifact.json"}
    morning = json.loads(MORNING_CHANGES.read_text())["playbook_fixtures"]
    assert set(morning) == {"tests/fixtures/playbooks/v2/morning-report/artifact.json"}
    hourly = json.loads(
        (ROOT / "tests/fixtures/contracts/discord-report-contract-changes.json").read_text()
    )["playbook_fixtures"]
    assert set(hourly) == {"tests/fixtures/playbooks/v2/supervisor-hourly-report/artifact.json"}
    narrative = json.loads(
        (ROOT / "tests/fixtures/contracts/hourly-narrative-policy-changes.json").read_text()
    )["playbook_fixtures"]
    assert narrative.keys() == hourly.keys()
    task_list_limit = _task_list_limit_changes()["playbook_fixtures"]
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
        if path in hibernation:
            assert hibernation[path]["previous_sha256"] == digest
            assert hibernation[path]["sha256"] != digest
            digest = hibernation[path]["sha256"]
        if path in probe_port:
            assert probe_port[path]["previous_sha256"] == digest
            historical = ROOT / probe_port[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert probe_port[path]["sha256"] != digest
            digest = probe_port[path]["sha256"]
        if path in object_loop_port:
            change = object_loop_port[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            ported = ROOT / "tests/fixtures/playbooks/object-loop-port/ported.artifact.json"
            assert hashlib.sha256(ported.read_bytes()).hexdigest() == change["sha256"]
            # The two reviewed recordings derive independently from this baseline.
            # Keep the authoring port intact while the filter recording is current.
        if path in object_loop:
            assert object_loop[path]["previous_sha256"] == digest
            assert object_loop[path]["sha256"] != digest
            digest = object_loop[path]["sha256"]
        if path in failover:
            assert failover[path]["previous_sha256"] == digest
            historical = ROOT / failover[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert failover[path]["sha256"] != digest
            digest = failover[path]["sha256"]
        if path in triage:
            assert triage[path]["previous_sha256"] == digest
            historical = ROOT / triage[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert triage[path]["sha256"] != digest
            digest = triage[path]["sha256"]
        if path in morning:
            assert morning[path]["previous_sha256"] == digest
            historical = ROOT / morning[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert morning[path]["sha256"] != digest
            digest = morning[path]["sha256"]
        if path in hourly:
            assert hourly[path]["previous_sha256"] == digest
            assert hourly[path]["sha256"] != digest
            digest = hourly[path]["sha256"]
        if path in narrative:
            change = narrative[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        if path in continuous:
            change = continuous[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            digest = change["sha256"]
        if path in promotion_request:
            change = promotion_request[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        if path in routing:
            assert routing[path]["previous_sha256"] == digest
            historical = ROOT / routing[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert routing[path]["sha256"] != digest
            digest = routing[path]["sha256"]
        if path in pipeline:
            assert pipeline[path]["previous_sha256"] == digest
            historical = ROOT / pipeline[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert pipeline[path]["sha256"] != digest
            digest = pipeline[path]["sha256"]
        if path in held_pipeline:
            change = held_pipeline[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        if path in composition:
            change = composition[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        if path in task_list_limit:
            change = task_list_limit[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            digest = change["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_hourly_narrative_policy_preserves_executable_graph_and_contract():
    evidence = json.loads(
        (ROOT / "tests/fixtures/contracts/hourly-narrative-policy-changes.json").read_text()
    )
    path = "tests/fixtures/playbooks/v2/supervisor-hourly-report/artifact.json"
    change = evidence["playbook_fixtures"][path]
    old = json.loads((ROOT / change["preserved_previous_path"]).read_bytes())
    new = json.loads((ROOT / path).read_bytes())
    for artifact in (old, new):
        for metadata in ("compiled_at", "source_hash"):
            artifact.pop(metadata)
        for node in [*artifact["rules"], *artifact["steps"].values()]:
            node.pop("source")
    assert new == old


def test_watch_reminder_additions_match_explicit_pins():
    task_list_limit = _task_list_limit_changes()["playbook_fixtures"]
    for path, digest in _watch_changes()["new_playbook_fixtures"].items():
        if path in task_list_limit:
            change = task_list_limit[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            digest = change["sha256"]
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


def _dev_hibernation_changes():
    changes = json.loads(DEV_HIBERNATION_CHANGES.read_text())
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


def _revert_target_repair_catalogue(catalogue):
    """Remove the new repair command and recover the preceding catalogue bytes."""
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
    delta = _dev_hibernation_changes()["catalogue_delta"]
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
        _revert_supervisor_health_catalogue(_dev_catalogue())
    )
    [(prop, schema)] = changes["added_property"].items()
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "task_set"]
    assert definition["input_schema"]["properties"].pop(prop) == schema
    assert prop not in definition["input_schema"].get("required", ())
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


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
    _revert_target_repair_catalogue(_dev_catalogue())


CONTINUOUS_CHANGES = ROOT / "tests/fixtures/contracts/promotion-continuous-contract-changes.json"



def test_original_pr_1842_legacy_playbook_fixtures_are_unchanged():
    # Preserve the baseline and every predecessor while applying reviewed ports.
    test_legacy_playbook_fixtures_are_unchanged()


POOL_DELETE_CHANGES = ROOT / "tests/fixtures/contracts/pool-delete-contract-changes.json"


def _pool_delete_changes():
    return json.loads(POOL_DELETE_CHANGES.read_text())


def _before_pool_delete_catalogue(catalogue=None):
    evidence = _pool_delete_changes()
    catalogue = _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue
    definitions = catalogue["explicit"]
    index = next(i for i, row in enumerate(definitions) if row["name"] == evidence["command"])
    assert definitions.pop(index) == evidence["transport_definition"]
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["category"]
    return catalogue


def test_pool_delete_catalogue_only_adds_reviewed_command():
    evidence = _pool_delete_changes()
    catalogue = _before_wait_requeue_catalogue()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


def test_pool_delete_original_catalogue_evidence():
    evidence = _pool_delete_changes()
    catalogue = _before_wait_requeue_catalogue()
    template = json.loads(render_command_catalogue())
    explicit = {row["name"]: row for row in catalogue["explicit"]}
    explicit[evidence["command"]] = evidence["transport_definition"]
    catalogue["explicit"] = [explicit[row["name"]] for row in template["explicit"]
                             if row["name"] in explicit]
    categories = {**catalogue["tool_categories"], evidence["command"]: evidence["category"]}
    catalogue["tool_categories"] = {name: categories[name] for name in template["tool_categories"]
                                    if name in categories}
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]


def _before_parallel_catalogue():
    catalogue = _before_held_ingestion_pipeline_catalogue()
    catalogue = _before_updater_catalogue(catalogue)
    catalogue = _before_pool_delete_catalogue(catalogue)
    catalogue = _before_retention_catalogue(catalogue)
    catalogue = _before_held_release_catalogue(catalogue)
    # These sibling PRs changed existing entries on different frozen bases.
    # Undo their reviewed values before applying workspace's complete ordering.
    for changes in (_git_first_changes(), _obsolete_child_changes()):
        for section, evidence in changes["catalogue_sections"].items():
            if not evidence["entries"]:
                continue
            entries = ({row["name"]: row for row in catalogue[section]}
                       if evidence["list"] else catalogue[section])
            for name, change in evidence["entries"].items():
                assert entries[name] == change["current"]
                assert change["previous"] is not None
                entries[name].clear()
                entries[name].update(change["previous"])
    catalogue = _before_workspace_catalogue(catalogue)
    return catalogue


GIT_FIRST_CHANGES = ROOT / "tests/fixtures/contracts/git-first-active-contract-changes.json"



HELD_RELEASE_CHANGES = ROOT / "tests/fixtures/contracts/held-release-contract-changes.json"


OBSOLETE_CHILD_CHANGES = ROOT / "tests/fixtures/contracts/obsolete-child-contract-changes.json"



WORKSPACE_CHANGES = ROOT / "tests/fixtures/contracts/workspace-ownership-contract-changes.json"


def _git_first_changes():
    return json.loads(GIT_FIRST_CHANGES.read_text())



def _obsolete_child_changes():
    changes = json.loads(OBSOLETE_CHILD_CHANGES.read_text())
    assert changes["source_base"] == "feeb89e65e4bb5e2a4a2033fa32f967d1f66fce9"
    assert set(changes["contracts"]) == {"integration_redrive_child"}
    assert set(changes["transport_definitions"]) == {"integration_redrive_child"}
    return changes



HELD_RELEASE_MERGE_CHANGES = ROOT / "tests/fixtures/contracts/held-release-dev-merge-contract-changes.json"


def _before_obsolete_child_catalogue():
    # Its frozen fallback order predates the parallel wait/restart changes.
    # Undo reviewed sibling values while retaining that exact ordering.
    return _before_parallel_catalogue()



def test_git_first_catalogue_only_updates_delivery_mode_description():
    evidence = _git_first_changes()
    change = evidence["catalogue_sections"]["explicit"]["entries"]["edit_project"]
    previous, current = change["previous"], change["current"]
    previous["input_schema"]["properties"]["hierarchical_integration_mode"]["description"] = (
        current["input_schema"]["properties"]["hierarchical_integration_mode"]["description"]
    )
    assert previous == current
    restored = _before_wait_requeue_catalogue()
    assert hashlib.sha256((json.dumps(restored, indent=2) + "\n").encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )



def _workspace_changes():
    return json.loads(WORKSPACE_CHANGES.read_text())


def test_obsolete_child_contract_only_adds_pinned_disposition():
    changes = _obsolete_child_changes()
    old = _dev_changes()["contracts"]["integration_redrive_child"]
    assert changes["contracts"]["integration_redrive_child"]["previous_fingerprint"] == (
        old["fingerprint"]
    )
    old = old["execution"]
    optional = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    old["args_schema"]["properties"].update(
        dispose_obsolete={"default": False, "type": "boolean"},
        superseded_by=optional, expected_parent_head_sha=optional,
    )
    position = old["receipt_projection"].index("tree_sha")
    for field in ("parent_head_sha", "merge_head_sha", "superseded_by"):
        old["result_schema"]["properties"][field] = optional
        old["receipt_projection"].insert(position, field)
        position += 1
    assert changes["contracts"]["integration_redrive_child"]["execution"] == old
    previous = json.dumps(_before_obsolete_child_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        changes["previous_cli_catalogue_sha256"]
    )



def test_original_pr_1872_legacy_execution_documents_and_fingerprints():
    # Apply every independently reviewed successor to the historical baseline.
    test_legacy_execution_documents_and_fingerprints()


def _before_workspace_catalogue(catalogue=None):
    return _revert_catalogue(
        _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue,
        _workspace_changes(),
    )


def test_original_pr_1789_legacy_execution_documents_and_fingerprints():
    # Apply every independently reviewed successor to the historical baseline.
    test_legacy_execution_documents_and_fingerprints()


def test_original_pr_1872_transport_views_and_cli_serialization_preserve_golden_evidence():
    # All parallel reviewed successors compose on this same golden baseline.
    test_transport_views_and_cli_serialization_preserve_golden_evidence()


def _held_release_changes():
    changes = json.loads(HELD_RELEASE_CHANGES.read_text())
    assert changes["source_base"] == "2fcad876c53308a9d3b0e104258aeb2f4f50f6ce"
    assert set(changes["transport_definitions"]) == {"doctor"}
    assert set(changes["added_contracts"]) == {
        "spec_ingest_legacy_adopt", "task_batch_release", "task_batch_retire",
    }
    return changes


def test_original_pr_1789_transport_views_and_cli_serialization_preserve_golden_evidence():
    # All parallel reviewed successors compose on this same golden baseline.
    test_transport_views_and_cli_serialization_preserve_golden_evidence()


def _held_release_merge_changes():
    changes = json.loads(HELD_RELEASE_MERGE_CHANGES.read_text())
    assert changes["source_base"] == "94e69a6a60eaa640ee3cae13dd21edffb1ffc2c7"
    assert changes["merged_source"] == "ddc3e6e6d9ba2ffa9af6fb1da041fa55115d2c5a"
    return changes


def test_original_pr_1789_obsolete_child_contract_only_adds_pinned_disposition():
    # Obsolete-child and PR-repair snapshots have distinct frozen ordering;
    # preserve each recorded digest rather than equating their source bases.
    test_obsolete_child_contract_only_adds_pinned_disposition()



def test_workspace_catalogue_only_changes_guarded_ownership_controls():
    evidence = _workspace_changes()
    snapshot = command_snapshot()
    assert set(evidence["transport_definitions"]) == {"release_workspace"}
    assert set(evidence["new_commands"]) == {"rebind_workspace"}
    for name, approved in evidence["new_commands"].items():
        assert _digest(snapshot.commands[name].tool_definition()) == approved["transport_sha256"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    previous = _before_restart_hold_catalogue()
    assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


def _retention_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/operational-retention-contract-changes.json").read_text()
    )


def _revert_held_release_catalogue(catalogue):
    """Remove exactly the reviewed held-release delta, leaving every other entry in place."""
    delta = _held_release_changes()["catalogue_delta"]
    explicit = {entry["name"]: entry for entry in catalogue["explicit"]}
    assert set(delta["previous_explicit"]) == {"doctor"}
    assert set(delta["added_explicit"]) == {"spec_ingest_legacy_adopt"}
    for name, entry in delta["added_explicit"].items():
        assert explicit[name] == entry
    catalogue["explicit"] = [
        delta["previous_explicit"].get(entry["name"], entry)
        for entry in catalogue["explicit"] if entry["name"] not in delta["added_explicit"]
    ]
    assert set(delta["added_fallback"]) == {"task_batch_release", "task_batch_retire"}
    assert set(delta["added_tool_categories"]) == {"spec_ingest_legacy_adopt"}
    for key in ("fallback", "tool_categories"):
        for name, entry in delta[f"added_{key}"].items():
            assert catalogue[key].pop(name) == entry
    return catalogue


def _before_held_release_catalogue(catalogue=None):
    return _revert_held_release_catalogue(
        _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue,
    )


def _before_retention_catalogue(catalogue=None):
    evidence = _retention_changes()
    catalogue = _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue
    entries = [e for e in catalogue["explicit"] if e.get("name") == evidence["command"]]
    assert entries == [evidence["explicit_entry"]]
    catalogue["explicit"].remove(entries[0])
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_held_release_merge_only_adds_reviewed_delta_to_dev():
    held = _held_release_changes()
    merge = _held_release_merge_changes()
    assert merge["source_cli_catalogue_sha256"] == held["cli_catalogue_sha256"]
    assert merge["previous_cli_catalogue_sha256"] == _pr_repair_changes()["cli_catalogue_sha256"]
    # Removing exactly the held-release delta recovers dev's frozen catalogue bytes.
    previous = json.dumps(_before_wait_requeue_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == merge["previous_cli_catalogue_sha256"]
    snapshot = command_snapshot()
    # Reverse the separately reviewed transport promotion before checking this historical pin.
    catalogue = _before_held_ingestion_pipeline_catalogue()
    for name, approved in held["added_contracts"].items():
        transport = catalogue["fallback"].get(name, snapshot.commands[name].tool_definition())
        assert _digest(transport) == approved["transport_sha256"]
        if "execution" in approved:
            contract = CONTRACTS.require(name).contract
            assert canonical_execution_document(contract.execution) == approved["execution"]
            assert contract.fingerprint() == approved["fingerprint"]


def test_retention_catalogue_only_adds_reviewed_command():
    evidence = _retention_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    catalogue = _before_wait_requeue_catalogue()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


def _supervisor_say_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/supervisor-say-contract-changes.json").read_text()
    )


def _before_supervisor_say_catalogue():
    catalogue = _before_task_list_limit_catalogue(
        _before_unread_inbox_catalogue(_before_wait_source_redispatch_catalogue())
    )
    evidence = _supervisor_say_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_supervisor_say_catalogue_only_adds_initiated_conversation_post():
    evidence = _supervisor_say_changes()
    assert evidence["command"] == "supervisor_inbox_say"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    catalogue = json.dumps(
        _before_task_list_limit_catalogue(
            _before_unread_inbox_catalogue(_before_wait_source_redispatch_catalogue())
        ),
        indent=2,
    ) + "\n"
    assert hashlib.sha256(catalogue.encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_supervisor_say_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]


def test_retention_original_catalogue_evidence():
    evidence = _retention_changes()
    catalogue = _before_wait_requeue_catalogue()
    template = json.loads(render_command_catalogue())
    explicit = {row["name"]: row for row in catalogue["explicit"]}
    explicit[evidence["command"]] = evidence["explicit_entry"]
    catalogue["explicit"] = [explicit[row["name"]] for row in template["explicit"]
                             if row["name"] in explicit]
    categories = {**catalogue["tool_categories"], evidence["command"]: evidence["tool_category"]}
    catalogue["tool_categories"] = {name: categories[name] for name in template["tool_categories"]
                                    if name in categories}
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
def _delivered_design_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/delivered-design-settlement-contract-changes.json").read_text()
    )


def _before_delivered_design_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = _before_supervisor_say_catalogue()
    evidence = _delivered_design_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "task_batch_propose"]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_delivered_design_catalogue_only_adds_explicit_settlement():
    evidence = _delivered_design_changes()
    # The independent schedule delta is absent from this frozen dev recording.
    catalogue = _before_project_schedules_catalogue()
    current = json.dumps(catalogue, indent=2) + "\n"
    previous = json.dumps(_before_delivered_design_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    assert hashlib.sha256(current.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    definition = json.loads(json.dumps(evidence["definition"]))
    settlement = definition["input_schema"]["properties"].pop("no_op")
    assert settlement["required"] == ["reason", "delivered_evidence"]
    assert settlement["properties"]["delivered_evidence"]["minItems"] == 1
    definition["description"] = evidence["previous_definition"]["description"]
    assert definition == evidence["previous_definition"]


def _held_ingestion_pipeline_transport_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/held-ingestion-pipeline-transport-changes.json").read_text()
    )


def _before_held_ingestion_pipeline_catalogue():
    return _revert_catalogue(
        _before_delivered_design_catalogue(_before_project_schedules_catalogue()),
        _held_ingestion_pipeline_transport_changes(),
    )


def test_held_ingestion_pipeline_only_promotes_three_reviewed_transports():
    evidence = _held_ingestion_pipeline_transport_changes()
    names = {"task_batch_review_record", "task_batch_release", "task_batch_retire"}
    for section in ("explicit", "fallback", "tool_categories"):
        assert set(evidence["catalogue_sections"][section]["entries"]) == names
    previous = json.dumps(_before_held_ingestion_pipeline_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    for path, change in evidence["playbook_fixtures"].items():
        baseline = json.loads(PIPELINE_CHANGES.read_text())["playbook_fixtures"][path]
        assert change["previous_sha256"] == baseline["sha256"]
        previous = (ROOT / change["preserved_previous_path"]).read_bytes()
        assert hashlib.sha256(previous).hexdigest() == change["previous_sha256"]
        composition = json.loads((
            ROOT / "tests/fixtures/contracts/default-pipeline-wait-grants-composition.json"
        ).read_text())["playbook_fixtures"][path]
        assert composition["previous_sha256"] == change["sha256"]
        held = ROOT / composition["preserved_previous_path"]
        assert hashlib.sha256(held.read_bytes()).hexdigest() == change["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == composition["sha256"]


def _project_schedule_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/project-schedule-contract-changes.json").read_text()
    )


def _before_project_schedules_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = _before_supervisor_say_catalogue()
    return _revert_catalogue(catalogue, _project_schedule_changes())


def test_project_schedules_only_add_scoped_notification_commands():
    evidence = _project_schedule_changes()
    names = {"schedule_register", "schedule_get", "schedule_list", "schedule_cancel"}
    assert set(evidence["contracts"]) == names
    sections = evidence["catalogue_sections"]
    assert set(sections["explicit"]["entries"]) == names
    assert set(sections["tool_categories"]["entries"]) == names
    assert set(sections["fallback"]["entries"]) == {"reconcile_project_schedules"}
    assert set(sections["categories"]["entries"]) == {"schedule"}
    for section in sections.values():
        assert all(entry["previous"] is None for entry in section["entries"].values())
    for name, approved in evidence["contracts"].items():
        contract = CONTRACTS.require(name).contract
        assert contract.fingerprint() == approved["fingerprint"]
        assert canonical_execution_document(contract.execution) == approved["execution"]
    # The independent delivered-design delta is absent from this task's recording.
    catalogue = _before_project_schedules_catalogue(_before_delivered_design_catalogue())
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
def _unread_inbox_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/unread-inbox-contract-changes.json").read_text()
    )


def _before_unread_inbox_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = json.loads(render_command_catalogue())
    approved = _unread_inbox_changes()["transport_definitions"]["message_inbox"]
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "message_inbox"]
    assert definition == approved["current"]
    definition.clear()
    definition.update(approved["previous"])
    return catalogue


def test_unread_inbox_only_changes_transport_description():
    evidence = _unread_inbox_changes()
    assert set(evidence["transport_definitions"]) == {"message_inbox"}
    approved = evidence["transport_definitions"]["message_inbox"]
    assert {**approved["previous"], "description": approved["current"]["description"]} == (
        approved["current"]
    )
    assert _digest(approved["previous"]) == approved["previous_sha256"]
    assert _digest(approved["current"]) == approved["sha256"]
    catalogue = _before_task_list_limit_catalogue()
    rendered = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_unread_inbox_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
def _wait_source_redispatch_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/wait-source-redispatch-contract-changes.json").read_text()
    )


def _before_wait_source_redispatch_catalogue():
    catalogue = json.loads(render_command_catalogue())
    evidence = _wait_source_redispatch_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_wait_source_redispatch_only_adds_scoped_recovery_command():
    evidence = _wait_source_redispatch_changes()
    assert evidence["command"] == "wait_source_redispatch"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    catalogue = _before_task_list_limit_catalogue(_before_unread_inbox_catalogue())
    rendered = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    previous = json.dumps(
        _before_task_list_limit_catalogue(
            _before_unread_inbox_catalogue(_before_wait_source_redispatch_catalogue())
        ),
        indent=2,
    ) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]


def _task_list_limit_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/task-list-limit-contract-changes.json").read_text()
    )


def _before_task_list_limit_catalogue(catalogue=None):
    # Compose independent catalogue deltas while preserving their frozen hashes.
    if catalogue is None:
        catalogue = _before_wait_source_redispatch_catalogue()
    evidence = _task_list_limit_changes()["transport_definitions"]["list_tasks"]
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "list_tasks"]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_task_list_limit_only_adds_optional_positive_limit():
    evidence = _task_list_limit_changes()
    assert set(evidence["contracts"]) == set(evidence["transport_definitions"]) == {"list_tasks"}
    previous = _observation_changes()["contracts"]["list_tasks"]
    approved = evidence["contracts"]["list_tasks"]
    assert approved["previous_fingerprint"] == previous["fingerprint"]
    contract = CONTRACTS.require("list_tasks").contract
    assert contract.fingerprint() == approved["fingerprint"] != previous["fingerprint"]
    assert canonical_execution_document(contract.execution) == approved["execution"]
    execution = json.loads(json.dumps(approved["execution"]))
    limit = execution["args_schema"]["properties"].pop("limit")
    assert limit == {
        "anyOf": [{"exclusiveMinimum": 0, "type": "integer"}, {"type": "null"}],
        "default": None,
    }
    assert execution == previous["execution"]
    transport = evidence["transport_definitions"]["list_tasks"]
    definition = json.loads(json.dumps(transport["definition"]))
    limit = definition["input_schema"]["properties"].pop("limit")
    assert limit["type"] == "integer" and limit["minimum"] == 1
    assert definition == transport["previous_definition"]
    assert _digest(transport["definition"]) == transport["sha256"]
    assert _digest(transport["previous_definition"]) == transport["previous_sha256"]
    catalogue = _before_unread_inbox_catalogue(_before_wait_source_redispatch_catalogue())
    rendered = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_task_list_limit_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]


def test_task_list_limit_playbooks_only_refresh_command_pins():
    evidence = _task_list_limit_changes()
    assert set(evidence["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/review-pipeline.artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/artifact.sha256",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/manifest.md",
    }
    contract = evidence["contracts"]["list_tasks"]
    for path, change in evidence["playbook_fixtures"].items():
        old_bytes = (ROOT / change["preserved_previous_path"]).read_bytes()
        new_bytes = (ROOT / path).read_bytes()
        assert hashlib.sha256(old_bytes).hexdigest() == change["previous_sha256"]
        assert hashlib.sha256(new_bytes).hexdigest() == change["sha256"]
        if not path.endswith(".json"):
            continue
        old, new = json.loads(old_bytes), json.loads(new_bytes)
        assert old["compiled_against"]["commands"].pop("list_tasks") == (
            contract["previous_fingerprint"]
        )
        assert new["compiled_against"]["commands"].pop("list_tasks") == contract["fingerprint"]
        old.pop("compiled_at")
        new.pop("compiled_at")
        assert new == old, path
