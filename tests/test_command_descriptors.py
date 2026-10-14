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
    GeneratedInvocationDisabled, JsonObjectResult, argument_model, generated_registration,
)
from src.commands.contracts.models import canonical_execution_document
from src.commands.contracts.registry import ContractRegistry
from src.commands.descriptors import (
    PLAYBOOK_DENIALS, HandlerCommand, PluginCommand, argument_usage,
    build_descriptor_snapshot, discover_handler_inventory,
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


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def _snapshot(*, handlers=(), explicit=(), fallback=None, typed=None, plugins=()):
    return build_descriptor_snapshot(
        handlers=handlers, explicit_definitions=explicit, fallback_schemas=fallback or {},
        typed_contracts=typed or {}, plugins=plugins,
    )


def test_legacy_execution_documents_and_fingerprints():
    fixture = json.loads(GOLDEN.read_text())
    changes = _observation_changes()["contracts"]
    for name, old in fixture["legacy_contracts"].items():
        expected = changes.get(name, old)
        if name in changes:
            assert expected["previous_fingerprint"] == old["fingerprint"]
            assert expected["fingerprint"] != old["fingerprint"]
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
    snapshot = command_snapshot()
    for name, digest in fixture["transport_definitions"].items():
        if name in changes["contracts"]:
            approved = changes["contracts"][name]
            assert approved["previous_transport_sha256"] == digest
            digest = approved["transport_sha256"]
        assert _digest(snapshot.commands[name].tool_definition()) == digest, name
    actual = hashlib.sha256(render_command_catalogue().encode()).hexdigest()
    assert changes["previous_cli_catalogue_sha256"] == fixture["cli_catalogue_sha256"]
    assert actual == changes["cli_catalogue_sha256"]
    definitions = {d["name"]: d for d in effective_tool_definitions()}
    for name, definition in _discover_all_commands().items():
        assert definition == snapshot.commands[name].tool_definition()
        if name in definitions:
            assert definition == definitions[name]


def test_legacy_playbook_fixtures_are_unchanged():
    changes = _observation_changes()["playbook_fixtures"]
    assert set(changes) == {
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json",
    }
    for path, digest in json.loads(GOLDEN.read_text())["playbook_fixtures"].items():
        if path in changes:
            assert changes[path]["previous_sha256"] == digest
            digest = changes[path]["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


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
        BindingRef, ResolutionScope, ValueResolutionError, resolve_value,
    )

    # The expression layer's missing marker cannot be coerced into JSON or
    # silently supplied as a default by the generated argument model.
    with pytest.raises(ValueResolutionError):
        value = resolve_value(BindingRef(binding="result", path="missing"),
                              ResolutionScope(bindings={"result": {"known": 1}}))
        json_args.model_validate({"count": value, "choice": "yes"})
