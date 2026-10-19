"""Operator authority keeps a real worker claim, audit and human decision fences."""

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.operator_authority import (
    OPERATOR_READS, OPERATOR_WRITES, OperatorGrant, arguments_sha256, operator_grant,
    operator_grant_var,
)
from src.commands.principal import ExecutionPrincipal, PrincipalKind, current_principal, principal_context
from src.database.tables import events
from src.git.github_contracts import GitHubCredentialIdentity, GitHubRepositoryBinding
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus, TaskType
from src.operator_decisions import OperatorDecisions
from src.profiles.capabilities import CapabilityPolicy
from tests.test_review_service import decided


@pytest.fixture
async def operator(command_handler_factory, monkeypatch):
    handler = await command_handler_factory()
    db = handler.db
    commands = sorted(OPERATOR_READS | OPERATOR_WRITES | {
        "operator_execute", "operator_preflight", "task_comment", "message_send", "decision_record",
    })
    profile = AgentProfile(id="ops", name="Ops", harness="codex", default_class="deep-high",
                           lifecycle="pool", aq_commands=commands, harness_tools=[], plugin_tools=[])
    await db.create_profile(profile)
    await db.create_project(Project(id="p", name="Project"))
    await db.create_project(Project(id="foreign", name="Foreign"))
    await db.create_agent(Agent(id="a", name="Operator", profile_id=profile.id,
                               state=AgentState.BUSY))
    await db.create_task(Task(id="t", project_id="p", title="Setup", description="",
                             status=TaskStatus.IN_PROGRESS, assigned_agent_id="a",
                             task_type=TaskType.OPERATOR, claim_epoch=7, profile_id=profile.id,
                             route_source="router"))
    await db.update_task("t", claim_epoch=7)
    await db.update_agent("a", current_task_id="t")
    session = SessionRecord(id="s", project_id="p", profile_id=profile.id, harness="codex",
                            provider="openai", name="ops", lifecycle="pool", work_dir="/tmp",
                            epoch="epoch", instance_token="instance", started_at=time.time(),
                            state="running", task_id="t", agent_id="a", last_claim_epoch=7,
                            claim_phase="active",
                            intelligence_class="deep-high")
    await db.create_session(session)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="s", session_instance_token="instance",
        project_id="p", profile_id=profile.id,
        policy=CapabilityPolicy.from_namespaces(aq_commands=commands),
    )
    monkeypatch.setattr(handler, "_cmd_provider_status", AsyncMock(return_value={"success": True}))
    handler.config.messages.enabled = True
    yield handler, principal
    await db.close()


async def execute(operator, name, **args):
    handler, principal = operator
    with principal_context(principal):
        return await handler.execute(name, args)


async def test_operator_api_surface_injects_identity_without_admitting_raw_controls(operator):
    args = {"command": "integration_status", "arguments": {}}
    scope = RequestScope(kind="session", session_id="s", project_id="p")
    assert check_command_scope("operator_execute", args, scope) is None
    assert args["session_id"] == "s" and args["project_id"] == "p"
    assert check_command_scope("integration_cutover", {}, scope) is not None
    result = await execute(operator, "operator_execute", **args)
    assert result.get("operator_task_id") == "t", result


def test_operator_task_kind_is_accepted_on_cli_and_task_tool_schemas():
    from src.cli.styles import TASK_TYPES
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    assert "operator" in TASK_TYPES
    for command in ("create_task", "edit_task"):
        definition = next(d for d in _ALL_TOOL_DEFINITIONS if d["name"] == command)
        assert "operator" in definition["input_schema"]["properties"]["task_type"]["enum"]


@pytest.mark.parametrize("command", [
    "review_decide", "review_delegate", "decision_record", "promote_approve", "delivery_promote",
    "delete_project", "provider_set", "update_config", "git_merge", "operator_execute",
])
async def test_operator_cannot_invoke_human_or_unbounded_actions(operator, command):
    result = await execute(operator, "operator_execute", command=command)
    assert not result["success"] and "outside" in result["error"]
    handler, _ = operator
    rows = await handler.db.get_recent_events(event_type="operator.refused", task_id="t")
    assert rows and json.loads(rows[0]["payload"])["command"] == command


@pytest.mark.parametrize("change", ["instance", "claim", "profile", "class", "task_kind", "state"])
async def test_operator_authority_uses_live_rows(operator, change):
    handler, principal = operator
    if change == "instance":
        principal = replace(principal, session_instance_token="old")
    elif change == "claim":
        await handler.db.update_session("s", last_claim_epoch=6)
    elif change == "state":
        await handler.db.update_session("s", desired_state="stopped")
    elif change == "task_kind":
        await handler.db.update_task("t", task_type=TaskType.FEATURE)
    elif change == "class":
        await handler.db.update_profile("ops", default_class="fast-high")
    else:
        await handler.db.update_profile("ops", aq_commands=["operator_preflight"])
    result = await execute((handler, principal), "operator_execute", command="provider_status")
    assert not result["success"], result


async def test_cross_project_and_non_setup_project_fields_are_refused(operator):
    for arguments in ({"project_id": "foreign"}, {"budget_limit": 200},
                      {"review_delegate_to": "supervisor"}, {"assignment_playbook_id": "router"}):
        result = await execute(operator, "operator_execute", command="edit_project", arguments=arguments)
        assert not result["success"], result


async def test_read_is_audited_as_session_and_nested_dispatch_has_no_grant(operator, monkeypatch):
    handler, _ = operator
    async def read(args):
        assert current_principal().kind is PrincipalKind.SESSION
        assert operator_grant().task_id == "t"
        nested = await handler.execute("decision_record", dict(
            object_kind="task", object_id="t", effect="note", operator="Jack",
            decision="Forged", source="cli", source_ref="test", idempotency_key="forged",
        ))
        assert not nested["success"]
        assert operator_grant().command == "integration_status"
        return {"success": True}
    monkeypatch.setattr(handler, "_cmd_integration_status", read)
    result = await execute(operator, "operator_execute", command="integration_status")
    assert result["success"] and operator_grant() is None
    rows = await handler.db.get_recent_events(event_type="operator.action", task_id="t")
    payload = json.loads(rows[0]["payload"])
    assert payload["session_id"] == "s" and payload["claim_epoch"] == 7
    assert "arguments" not in payload


async def test_no_action_runs_when_audit_cannot_persist(operator, monkeypatch):
    handler, _ = operator
    action = AsyncMock()
    monkeypatch.setattr(handler, "_cmd_provider_status", action)
    monkeypatch.setattr(handler.db, "log_event", AsyncMock(side_effect=RuntimeError("audit offline")))
    result = await execute(operator, "operator_execute", command="provider_status")
    assert "error" in result
    action.assert_not_awaited()


async def decision(handler, command, arguments, *, recorded_by="human:local-operator"):
    return await OperatorDecisions(handler.db).record(dict(
        object_kind="task", object_id="t", effect="note", operator="Jack", source="cli",
        source_ref="test", idempotency_key=recorded_by,
        decision=json.dumps({"command": command, "args_sha256": arguments_sha256(arguments),
                             "claim_epoch": 7}), releases=None,
    ), recorded_by=recorded_by)


async def test_destructive_action_needs_exact_human_decision_and_ready_preflight(operator, monkeypatch):
    handler, _ = operator
    action = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(handler, "_cmd_playbook_delete", action)
    arguments = {"playbook_id": "p.cleanup", "scope": "project", "scope_identifier": "p",
                 "artifact_sha256": "sha256:" + "a" * 64}
    supervisor = await decision(handler, "playbook_delete", arguments,
                                recorded_by="supervisor session:supervisor")
    result = await execute(operator, "operator_execute", command="playbook_delete",
                           arguments=arguments, decision_id=supervisor["id"])
    assert not result["success"] and "human decision" in result["error"]
    human = await decision(handler, "playbook_delete", arguments)
    result = await execute(operator, "operator_execute", command="playbook_delete",
                           arguments=arguments, decision_id=human["id"])
    assert not result["success"] and "preflight" in result["error"]
    plan = {"command": "playbook_delete", "arguments": arguments, "decision_id": human["id"]}
    preflight = await execute(operator, "operator_preflight", actions=[plan])
    assert preflight["ready"], preflight
    for changed in ({**arguments, "playbook_id": "p.changed"}, arguments):
        result = await execute(operator, "operator_execute", **{**plan, "arguments": changed},
                               preflight_id=preflight["preflight_id"])
        assert result.get("success") is (changed == arguments), result
    action.assert_awaited_once_with(arguments)
    async with handler.db._engine.begin() as conn:
        await conn.execute(update(events).where(events.c.id == preflight["preflight_id"])
                           .values(timestamp=time.time() - 601))
    result = await execute(operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"])
    assert not result["success"]


async def test_preflight_collects_options_apps_branches_and_runners_in_one_message(operator, monkeypatch):
    handler, _ = operator
    for command in ("integration_trust_manifest", "integration_app_verify"):
        monkeypatch.setattr(handler, "_cmd_" + command,
                            AsyncMock(return_value={"success": False, "error": command + " blocked"}))
    monkeypatch.setattr(handler.db, "get_repo", AsyncMock(return_value=SimpleNamespace(project_id="p")))
    client = SimpleNamespace(exact_head_ref=AsyncMock(return_value=None),
                             request_json=AsyncMock(return_value={"runners": []}),
                             credential_identity=GitHubCredentialIdentity.app(123))
    binding = GitHubRepositoryBinding(repository_id=99, full_name="owner/repo")
    handler.orchestrator.github_repository_binding_resolver = lambda repo: binding
    handler.orchestrator.github_client_factory = lambda binding: client
    result = await execute(operator, "operator_preflight", actions=[
        {"command": "provider_status", "arguments": {"unknown": True}},
        {"command": "review_decide"},
    ], branches=["missing"], runner_labels=["build"], app_ids=[456])
    assert result["success"] and not result["ready"]
    assert len(result["blockers"]) == 7, result
    assert "notification_error" not in result, result
    messages = await handler.db.list_messages(to_kind="user", to_id="dashboard")
    assert len(messages) == 1
    assert all(blocker in messages[0].body for blocker in result["blockers"])


async def test_operator_playbooks_need_project_scope_and_reviewed_exact_hash(operator):
    handler, _ = operator
    grant = OperatorGrant("playbook_activate", "digest", "p", "t", "s", 7)
    token = operator_grant_var.set(grant)
    try:
        assert await handler._operator_playbook_check("system", "", "hash", enabling=True)
        assert await handler._operator_playbook_check("project", "foreign", "hash")
        assert await handler._operator_playbook_check("project", "p", "hash", enabling=True)
        assert await handler._operator_playbook_check("project", "p", "hash", enabling=False) is None
    finally:
        operator_grant_var.reset(token)


@pytest.mark.parametrize("command", ["playbook_activate", "playbook_delete", "policy_apply", "pr_merge", "review_decide", "decision_record"])
async def test_raw_operator_commands_cannot_bypass_wrapper_even_with_capabilities_off(operator, command):
    handler, _ = operator
    handler.config.security.capability_enforcement = "off"
    result = await execute(operator, command)
    assert not result.get("success", "error" not in result), result


async def test_project_policy_copy_runs_through_preflight_and_blocks_global_placement(operator):
    from src.policy_profiles.models import Bundle, TemplatePolicy
    from src.policy_profiles.service import item

    handler, _ = operator
    entry = item("spec.base", "project", TemplatePolicy(kind="spec", content="# Setup draft\n"))
    bundle = Bundle(name="Setup", source_aq_version="test", items=[entry]).model_dump(mode="json")
    arguments = {"bundle": bundle}
    denied = await execute(operator, "operator_execute", command="policy_diff", arguments={
        **arguments, "selections": {entry.id: {"scope": "global"}},
    })
    assert not denied["success"] and "project scope" in denied["error"], denied
    preview = await execute(operator, "operator_execute", command="policy_diff", arguments=arguments)
    assert preview["success"], preview
    arguments["selections"] = {row["id"]: {"scope": "project", "expected_checksum": row["current_checksum"]}
                               for row in preview["items"]}
    plan = {"command": "policy_apply", "arguments": arguments}
    preflight = await execute(operator, "operator_preflight", actions=[plan])
    assert preflight["ready"], preflight
    copied = await execute(operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"])
    assert copied["success"], copied
    path = Path(handler.config.vault_root) / "projects/p/templates/spec/base.md"
    assert path.read_text() == "# Setup draft\n"
    assert not (Path(handler.config.vault_root) / "system/templates/spec/base.md").exists()


def setup_playbook_pin():
    from src.playbooks.definition import PlaybookDefinition, canonical_bytes, source_digest
    from src.reviews.service import PlaybookPin

    source = "# Scoped setup\n"
    reference = {"path": "setup.md", "start_line": 1, "end_line": 1}
    definition = PlaybookDefinition.model_validate({
        "id": "p.setup", "version": 1, "scope": {"type": "project", "project_id": "p"},
        "source_hash": source_digest(source), "compiled_at": "2026-10-10T00:00:00Z",
        "rules": [{"id": "setup", "name": "Setup", "trigger": {"event_type": "task.created"},
                   "entry_step": "done", "source": reference}],
        "steps": {"done": {"type": "terminal", "title": "Done", "rule": "setup", "outcome": "completed",
                           "source": reference}},
    })
    pin = PlaybookPin(meta={
        "playbook_id": definition.id, "artifact_sha256": definition.artifact_sha256(),
        "scope": "project", "scope_identifier": "p", "source_markdown": source,
        "activate_on_approval": False,
    }, artifact=canonical_bytes(definition).decode())
    return source, definition, pin


async def test_operator_activation_requires_approved_current_revision_and_exact_hash(operator):
    handler, _ = operator
    handler.config.playbooks.enabled = True
    source, definition, pin = setup_playbook_pin()
    sha = definition.artifact_sha256()
    service = handler._review_service()
    review = await service.submit(project_id="p", author_task_id="t", kind="other",
                                  title="Setup", content=source, submitted_by="session:s",
                                  playbook=pin)
    assert not await handler._operator_artifact_approved("p", sha)
    approved = await decided(service, review_id=review["review_id"], revision=1,
                             decision="approve", note="", decided_by="human:local-operator")
    assert approved["application_state"] == "applied" and approved["playbook"]["stored"], approved
    assert await handler._operator_artifact_approved("p", sha)
    assert not await handler._operator_artifact_approved("foreign", sha)
    assert not await handler._operator_artifact_approved("p", "sha256:" + "c" * 64)


def test_operator_cli_parses_action_objects_and_preflight_receipt(monkeypatch):
    from click.testing import CliRunner
    from src.cli import app

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = {"success": True}
    monkeypatch.setattr(app, "_get_client", lambda *_: client)
    result = CliRunner().invoke(app.cli, ["operator", "execute", "--command", "provider_status",
        "--arguments", '{"provider":"openai"}', "--preflight-id", "42"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("operator_execute", {
        "command": "provider_status", "arguments": {"provider": "openai"}, "preflight_id": 42,
    })


async def test_operator_imports_project_bundle_then_activates_only_after_approval(operator):
    import yaml
    from src.playbooks.definition import canonical_bytes

    handler, _ = operator
    handler.config.playbooks.enabled = True
    source, definition, pin = setup_playbook_pin()
    sha = definition.artifact_sha256()
    bundle = Path(handler.config.vault_root) / "reviewed" / definition.id
    bundle.mkdir(parents=True)
    (bundle / "artifact.json").write_bytes(canonical_bytes(definition))
    (bundle / "artifact.sha256").write_text(sha + "\n")
    (bundle / "source.md").write_text(source)
    manifest = {"playbook_id": definition.id, "artifact_sha256": sha,
                "source_sha256": definition.source_hash,
                "contract_fingerprint": definition.contract_fingerprint(), "profiles_referenced": []}
    (bundle / "manifest.md").write_text("---\n" + yaml.safe_dump(manifest) + "---\n")
    plan = {"command": "playbook_v2_import", "arguments": {"path": str(bundle)}}
    preflight = await execute(operator, "operator_preflight", actions=[plan])
    assert preflight["ready"], preflight
    imported = await execute(operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"])
    assert imported["success"] and not imported["activated"], imported
    plan = {"command": "playbook_activate", "arguments": {
        "playbook_id": definition.id, "artifact_sha256": sha,
    }}
    preflight = await execute(operator, "operator_preflight", actions=[plan])
    assert not preflight["ready"] and "approved review" in str(preflight["blockers"]), preflight
    service = handler._review_service()
    review = await service.submit(project_id="p", author_task_id="t", kind="other", title="Setup",
                                  content=source, submitted_by="session:s", playbook=pin)
    approved = await decided(service, review_id=review["review_id"], revision=1,
                             decision="approve", note="", decided_by="human:local-operator")
    assert approved["application_state"] == "applied" and approved["playbook"]["stored"], approved
    preflight = await execute(operator, "operator_preflight", actions=[plan])
    assert preflight["ready"], preflight
    activated = await execute(operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"])
    assert activated["success"] and not activated["blocked"], activated
    assert activated["activation"]["activated_by"] == "session:s"
