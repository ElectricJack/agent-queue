"""Generated return protocol, resource authority and producer-audit release evidence."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.commands.command_inventory import command_snapshot
from src.commands.contracts.generated import (
    GeneratedInvocationDisabled, argument_model, generated_registration,
)
from src.commands.descriptors import PLAYBOOK_DENIALS, CommandDescriptor
from src.commands.handler import CommandHandler
from src.commands.principal import (
    SERVER_OWNED_ARG_KEYS, ExecutionPrincipal, PrincipalKind, current_principal, principal_context,
)
from src.commands.resource_authorization import authorize_resources, resource_requirements
from src.config import AppConfig
from src.playbooks.definition import CommandStep
from src.playbooks.executors.command import _consume
from src.playbooks.expressions import LiteralValue
from src.playbooks.run_state import DEFAULT_MAX_RESULT_BYTES
from src.profiles.capabilities import DENY_ALL, CapabilityPolicy

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "tests/fixtures/contracts/generated-command-audit.json"


def descriptor(name="new_command", **kwargs):
    return CommandDescriptor(
        name=name, description="A generated command", origin="handler", available=True,
        plugin=None, schema_source="opaque", schema_quality="opaque",
        transport_schema_source="legacy_empty", result_mode="json_object", capability=name,
        _playbook_schema='{"type":"object","additionalProperties":true}', **kwargs,
    )


def principal(project="p"):
    return ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL, project_id=project)


def consume(value, *, bound=True):
    registration = generated_registration(descriptor())
    step = CommandStep(
        rule="r", title="Generated", source={"path": "test.md", "start_line": 1, "end_line": 1},
        command=registration.name, save_result_as="result" if bound else None,
        transitions={"success": "done", "failure": "bad", "runtime_error": "bad"},
    )
    return _consume(value, registration, step, SimpleNamespace(run_id="run"),
                    resolved_inputs={}, key="attempt")


def test_builtin_audit_is_complete():
    """New handlers and changed return branches require an explicit audit update."""
    rows = json.loads(AUDIT.read_text())
    snapshot = command_snapshot()
    assert rows.keys() == snapshot.handler_names
    for name, row in rows.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(CommandHandler, "_cmd_" + name))))
        digest = "sha256:" + hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
        assert row["source_digest"] == digest, name
        assert row["input_schema_source"] == snapshot.commands[name].schema_source, name
        assert row["effect"] == snapshot.commands[name].effect_class, name
        assert row["resource_gates"] == list(resource_requirements(name)), name
        assert row["return_branches"] and row["tests"], name
        if row["mode"] == "json_object":
            assert resource_requirements(name) != ("resource_audit_required",), name
        for branch in row["return_branches"]:
            assert branch["protocol"], (name, branch)
        for helper, expected in row.get("helper_producers", {}).items():
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(CommandHandler, helper))))
            digest = "sha256:" + hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
            assert digest == expected, (name, helper)


@pytest.mark.parametrize(("value", "outcome"), [
    ({"success": True, "error": "a business observation"}, "success"),
    ({"success": False, "payload": [1, "kept"]}, "failure"),
    ({"success": False, "error_code": "capability_denied"}, "unauthorized"),
    ({"success": False, "error": "out of scope: foreign task"}, "unauthorized"),
    ({"success": False, "error_code": "command_runtime_error"}, "runtime_error"),
    ({}, "contract_violation"), ({"error": None}, "contract_violation"),
    ({"success": 1}, "contract_violation"), ({"success": "true"}, "contract_violation"),
    ({"success": None}, "contract_violation"), ([], "contract_violation"),
    (None, "contract_violation"), ("yes", "contract_violation"),
    ({"success": True, "value": float("nan")}, "contract_violation"),
    ({"success": True, "value": float("inf")}, "contract_violation"),
    ({"success": True, "value": (1, 2)}, "contract_violation"),
    ({"success": True, "value": object()}, "contract_violation"),
    ({"success": True, 4: "non-string key"}, "contract_violation"),
])
def test_generic_result_protocol(value, outcome):
    result = consume(value)
    assert result.outcome == outcome
    if outcome != "contract_violation":
        assert result.value == value  # All existing keys, including error, are retained.
    else:
        assert result.value is None


@pytest.mark.parametrize("bound", [False, True])
def test_oversize_results_are_rejected_even_without_a_binding(bound):
    result = consume({"success": True, "body": "x" * DEFAULT_MAX_RESULT_BYTES}, bound=bound)
    assert result.outcome == "contract_violation"
    assert result.value is None


def test_failure_diagnostics_never_include_raw_handler_errors():
    result = consume({"success": False, "error": "secret-exception-sentinel"})
    assert result.diagnostics == ("command_failed",)
    assert "secret-exception-sentinel" not in str(result.receipt_result)


@pytest.mark.parametrize("key", SERVER_OWNED_ARG_KEYS)
def test_generated_args_reject_every_server_owned_key(key):
    model = argument_model(descriptor())
    with pytest.raises(ValidationError):
        model.model_validate({key: {"kind": "local"}})
    assert model.model_validate({"task_id": "ordinary-input"}).model_dump() == {
        "task_id": "ordinary-input",
    }


async def test_generated_execution_remains_disabled():
    registration = generated_registration(descriptor())
    with pytest.raises(GeneratedInvocationDisabled):
        await registration.invoke(registration.contract.execution.args_model.model_validate({}), principal())


@pytest.mark.parametrize("name", PLAYBOOK_DENIALS)
async def test_exact_denials_apply_even_to_direct_generated_invocation(name):
    registration = generated_registration(descriptor(name))
    args = registration.contract.execution.args_model.model_validate({})
    assert (await registration.invoke(args, principal(None)))["error_code"] == "playbook_command_denied"


@pytest.mark.parametrize("generated", [False, True])
async def test_project_filter_cannot_authorize_a_foreign_task(monkeypatch, generated):
    from src.api.target_scope import TARGET_RESOLVERS

    lookup = AsyncMock(return_value=(True, "other"))
    monkeypatch.setitem(TARGET_RESOLVERS, "task_id", lookup)
    result = await authorize_resources("get_task_result", {"project_id": "p", "task_id": "t"},
                                       principal(), db=object(), generated=generated)
    assert result["error_code"] == "resource_scope_denied"
    lookup.assert_awaited_once()


@pytest.mark.parametrize("generated", [False, True])
async def test_session_domain_gates_do_not_admit_other_system_resources(generated):
    caller = replace(principal(), kind=PrincipalKind.SESSION, session_id="worker")
    result = await authorize_resources("get_config", {}, caller, db=object(), generated=generated)
    assert result["error_code"] == "resource_scope_denied"


async def test_scoped_reads_inherit_project_and_global_reads_require_system():
    args = {}
    assert await authorize_resources("get_token_usage", args, principal(), db=object(), generated=True) is None
    assert args == {"project_id": "p"}
    result = await authorize_resources("get_config", {}, principal(), db=object(), generated=True)
    assert result["success"] is False
    assert await authorize_resources("get_config", {}, principal(None), db=object(), generated=True) is None


@pytest.mark.parametrize("command", ["task_claim", "task_close", "task_heartbeat", "review_decide"])
async def test_capabilities_do_not_create_live_session_or_decision_authority(command):
    forged = replace(principal(None), session_id="fake", elevated=True)
    result = await authorize_resources(command, {}, forged, db=object(), generated=True)
    assert result["success"] is False
    assert result["error_code"] in {"session_required", "unauthorized"}


async def test_nested_dispatch_keeps_original_principal_and_scope():
    handler = CommandHandler(SimpleNamespace(db=None, bus=SimpleNamespace(emit=AsyncMock()),
                                             plugin_registry=None), AppConfig())
    observed = []
    caller = principal()
    async def inner(args):
        observed.append((current_principal(), handler._current_scope))
        return {"success": True}
    handler._cmd_get_schema = inner
    # A schema read is allowed without data resources. Capability audit mode
    # still requires an unresolved legacy provenance for this narrow fixture.
    caller = replace(caller, provenance=("profile-not-found",))
    with principal_context(caller):
        await handler.execute("get_schema", {})
        assert (await handler.execute("get_schema", {"_scope": {"kind": "local"}}))["success"] is False
    assert observed[0][0] is caller
    assert observed[0][1]["project_id"] == "p"
    assert observed[0][1]["kind"] == "playbook"
    assert current_principal() is None


@pytest.mark.parametrize("kind", [PrincipalKind.SESSION, PrincipalKind.PLAYBOOK])
async def test_untrusted_dispatch_exceptions_keep_details_in_server_logs(kind, caplog):
    handler = CommandHandler(SimpleNamespace(db=None, bus=SimpleNamespace(emit=AsyncMock()),
                                             plugin_registry=None), AppConfig())
    caller = replace(principal(), kind=kind, policy=CapabilityPolicy.from_namespaces(
        aq_commands=["get_schema"],
    ))
    handler._cmd_get_schema = AsyncMock(side_effect=RuntimeError("private-exception-sentinel"))
    with principal_context(caller):
        result = await handler.execute("get_schema", {})
    assert result == {
        "success": False, "error_code": "command_runtime_error", "error": "Command execution failed",
    }
    assert "private-exception-sentinel" in caplog.text
    assert "private-exception-sentinel" not in repr(result)


async def test_project_paths_are_confined_to_owned_roots(tmp_path):
    own, foreign = tmp_path / "own", tmp_path / "other"
    own.mkdir()
    foreign.mkdir()
    db = SimpleNamespace(list_repos=AsyncMock(return_value=[]), list_workspaces=AsyncMock(return_value=[
        SimpleNamespace(project_id="p", workspace_path=str(own)),
        SimpleNamespace(project_id="other", workspace_path=str(foreign)),
    ]))
    handler = CommandHandler(SimpleNamespace(db=db, plugin_registry=None), AppConfig(workspace_dir=str(tmp_path)))
    with principal_context(principal()):
        assert await handler._validate_path(str(own / "file")) == str(own / "file")
        assert await handler._validate_path(str(foreign / "file")) is None
        (own / "link").symlink_to(foreign, target_is_directory=True)
        assert await handler._validate_path(str(own / "link" / "file")) is None


@pytest.mark.parametrize("name", PLAYBOOK_DENIALS)
def test_denied_commands_are_rejected_by_validation_and_hidden_from_llm(name):
    from src.playbooks.executors.llm import _published_tools
    from src.playbooks.validation import validate_definition
    from tests.playbook_v2_engine_helpers import minimal_artifact

    artifact = minimal_artifact()
    old = artifact.steps["ensure-review-task"]
    artifact = artifact.model_copy(update={"steps": {
        **artifact.steps, "ensure-review-task": old.model_copy(update={"command": name}),
    }})
    diagnostics = validate_definition(artifact)
    assert any(d.code == "unauthorized" for d in diagnostics)
    step = SimpleNamespace(tool_use=SimpleNamespace(enabled=True, aq_commands=[name], plugin_tools=[]))
    # Denials apply before catalogue and resolver lookups, even if registered.
    assert _published_tools(step, SimpleNamespace()) == []


@pytest.mark.parametrize("key", SERVER_OWNED_ARG_KEYS)
def test_validation_rejects_server_owned_inputs_for_typed_commands_too(key):
    from src.playbooks.validation import validate_definition
    from tests.playbook_v2_engine_helpers import minimal_artifact

    artifact = minimal_artifact()
    old = artifact.steps["ensure-review-task"]
    artifact = artifact.model_copy(update={"steps": {
        **artifact.steps, "ensure-review-task": old.model_copy(update={
            "inputs": {key: LiteralValue(value={})},
        }),
    }})
    assert any(d.code == "invalid_argument" for d in validate_definition(artifact))


async def test_artifacts_runs_reviews_and_sessions_use_persisted_owners():
    db = SimpleNamespace(
        get_playbook_artifact_row=AsyncMock(return_value={"scope": "project", "scope_identifier": "other"}),
        load_run=AsyncMock(return_value=SimpleNamespace(artifact_sha256="sha256:foreign")),
        get_review=AsyncMock(return_value={"project_id": "other"}),
        get_session=AsyncMock(return_value=SimpleNamespace(project_id="other")),
    )
    for name, args in [
        ("playbook_activate", {"artifact_sha256": "sha256:foreign"}),
        ("inspect_playbook_run", {"run_id": "run"}),
        ("review_show", {"review_id": "review"}),
        ("session_show", {"session_id": "session"}),
    ]:
        refusal = await authorize_resources(name, args, principal(), db=db, generated=True)
        assert refusal["success"] is False, name
        assert refusal["error_code"] == "resource_scope_denied", name


async def test_project_playbook_activation_uses_artifact_scope():
    db = SimpleNamespace(get_playbook_artifact_row=AsyncMock(return_value={
        "scope": "project", "scope_identifier": "p",
    }))
    assert await authorize_resources("playbook_activate", {
        "playbook_id": "review", "artifact_sha256": "sha256:owned",
    }, principal(), db=db, generated=True) is None


@pytest.mark.parametrize("key", ["session_id", "id", "name"])
async def test_session_aliases_cannot_read_foreign_rows(key):
    db = SimpleNamespace(get_session=AsyncMock(return_value=None),
                         get_session_by_name=AsyncMock(return_value=SimpleNamespace(project_id="other")))
    refusal = await authorize_resources("session_show", {key: "worker-other"}, principal(),
                                        db=db, generated=True)
    assert refusal["error_code"] == "resource_scope_denied"


async def test_tuple_helpers_produce_explicit_failure_envelopes():
    handler = CommandHandler(SimpleNamespace(db=None, plugin_registry=None), AppConfig())
    for name, args in [("session_show", {}), ("session_peek", {}),
                       ("read_prompt", {"path": "relative.md"}),
                       ("task_comment_delete", {})]:
        result = await getattr(handler, "_cmd_" + name)(args)
        assert result["success"] is False, (name, result)
        assert consume(result).outcome == "failure", name


@pytest.mark.parametrize("serialized", [False, True])
async def test_manual_run_event_cannot_override_project(serialized):
    foreign = {"project_id": "other"}
    args = {"playbook_id": "review", "event": json.dumps(foreign) if serialized else foreign}
    assert (await authorize_resources("run_playbook", args, principal(), db=object(),
                                      generated=True))["success"] is False
    args = {"playbook_id": "review"}
    assert await authorize_resources("run_playbook", args, principal(), db=object(),
                                     generated=True) is None
    assert args["event"] == {"project_id": "p"}


async def test_generated_runtime_exception_has_safe_executor_diagnostics():
    from src.commands.contracts.registry import ContractRegistry
    from tests.test_command_executor import command_step, context, run

    async def invoke(args, caller):
        raise RuntimeError("private-exception-sentinel")

    registration = replace(generated_registration(descriptor()), invoke=invoke)
    registry = ContractRegistry()
    registry.register(registration)
    result = await run(command_step(command=registration.name, transitions={"runtime_error": "bad"}),
                       context(registry, inputs={}, principal=principal()))
    assert result.outcome == "runtime_error"
    assert "private-exception-sentinel" not in repr(result)


@pytest.mark.parametrize(("tool_result", "step_outcome", "tool_code"), [
    ({"success": "true", "error": "private-malformed-result"}, "low", "contract_violation"),
    ({"success": False, "error_code": "resource_scope_denied"},
     "unauthorized", "resource_scope_denied"),
])
async def test_llm_generic_tools_use_the_same_result_boundary(tool_result, step_outcome, tool_code):
    from src.commands.contracts.registry import ContractRegistry
    from src.llm.fake import FakeProvider
    from src.llm.types import TokenUsage
    from src.playbooks.executors.llm import LiveLlmExecutor
    from tests.test_llm_executor import ProfileStore, context, llm_step, profile

    async def invoke(args, caller):
        return tool_result

    registration = replace(generated_registration(descriptor()), invoke=invoke)
    registry = ContractRegistry()
    registry.register(registration)
    provider = FakeProvider()
    provider.add_tool_call(registration.name, {}, usage=TokenUsage(10, 2, True))
    provider.add_text('{"risk":"low"}', usage=TokenUsage(10, 2, True))
    ctx = context(provider, db=ProfileStore(profile(aq_commands=[registration.name])))
    ctx = replace(ctx, services=replace(ctx.services, contracts=registry, resolver=SimpleNamespace(
        is_builtin=lambda name: True, is_plugin=lambda name: False,
        plugin_command_names=lambda: frozenset(),
    )))
    step = llm_step(tool_use={"enabled": True, "aq_commands": [registration.name]})
    result = await LiveLlmExecutor().execute(step, ctx)
    assert result.outcome == step_outcome
    assert tool_code in repr(provider.calls[1].messages)
    assert "private-malformed-result" not in repr(provider.calls[1].messages)


async def test_producer_flags_preserve_read_payloads_and_early_errors():
    db = SimpleNamespace(get_task_result=AsyncMock(return_value={"task_id": "t", "outcome": "fail"}))
    handler = CommandHandler(SimpleNamespace(db=db, plugin_registry=None), AppConfig())
    assert await handler._cmd_get_task_result({"task_id": "t"}) == {
        "success": True, "task_id": "t", "outcome": "fail",
    }
    db.get_task_result.return_value = None
    missing = await handler._cmd_get_task_result({"task_id": "t"})
    assert missing["success"] is False and "error" in missing
    assert (await handler._cmd_agent_message({}))["success"] is False
    schema = await handler._cmd_get_schema({})
    assert schema["success"] is True and len(schema) > 1


async def test_generated_adapter_preserves_principal_without_inferring_result(monkeypatch):
    from src.commands.contracts import generated
    from src.commands.contracts.builtin import set_handler_provider

    caller = principal()
    raw = {"a_plugin_forgot_success": True}
    seen = []
    async def execute(name, args):
        seen.append((name, args, current_principal()))
        return raw
    set_handler_provider(lambda: SimpleNamespace(execute=execute))
    monkeypatch.setattr(generated, "require_generated_execution", lambda: None)
    try:
        registration = generated_registration(replace(descriptor(), origin="plugin", plugin="probe"))
        result = await registration.invoke(
            registration.contract.execution.args_model.model_validate({"task_id": "t"}), caller,
        )
        assert result is raw
        assert seen == [("new_command", {"task_id": "t"}, caller)]
        assert consume(result).outcome == "contract_violation"
        assert current_principal() is None
    finally:
        set_handler_provider(None)
