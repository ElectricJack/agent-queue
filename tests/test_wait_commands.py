"""Verified wait surfaces, principal fences and dormant runtime composition."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from src.api.codegen import API_EXCLUDED, DETAILED_ERROR_COMMANDS
from src.api.models.wait import REQUEST_MODELS, WaitDecideResponse, WaitResponse
from src.api.scope import AGENT_COMMAND_SET
from src.api.target_scope import target_scope_error
from src.cli.auto_commands import EXCLUDED
from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.commands.contracts.wait import WaitRegisterArgs
from src.commands.contracts.wait_v2 import (
    PUBLIC_WAIT_V2_COMMANDS,
    SERVICE_WAIT_COMMANDS,
    WAIT_V2_ARGS,
    WaitCheckpointArgs,
    WaitDecideArgs,
    caller_allowed,
)
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    TRUSTED_LOCAL,
    principal_context,
)
from src.config import AppConfig
from src.database.queries.wait_queries import register_wait
from src.database.tables import (
    continuations, doc_reviews, events, messages, sessions, tasks, wait_deadlines,
    wait_records, wait_source_events,
)
from src.database.queries.continuation_queries import wait_outcome_envelope
from src.models import SessionRecord
from src.wait_runtime.service import WaitService
from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS
from src.profiles.capabilities import AQ_FEATURE_CAPABILITIES, CapabilityPolicy
from src.profiles.capability_sync import sync_profile_capabilities
from src.profiles.drift import diff_profile, shipped_profile_path, vault_profile_path
from src.tools.definitions import _ALL_TOOL_DEFINITIONS
from src.waits.legacy import translate_registration
from src.waits.conditions import Condition, Scope, WaitError
from src.waits.registry import build_registry
from tests.test_operator_workers import operator as _operator, execute as operator_execute
from tests.test_wait_queries import NOW, Policy, decide, env as _env, register

env = _env
operator = _operator
PUBLIC_AND_EXISTING = PUBLIC_WAIT_V2_COMMANDS | {
    "wait_register",
    "wait_get",
    "wait_list",
    "wait_cancel",
}
POLICY = CapabilityPolicy.from_namespaces(
    aq_commands=sorted(
        PUBLIC_AND_EXISTING | SERVICE_WAIT_COMMANDS | {"wait_legacy_bridge", "waits.fleet_summary"}
    )
)
WORKER = ExecutionPrincipal(
    kind=PrincipalKind.SESSION,
    policy=POLICY,
    session_id="s",
    session_instance_token="token",
    task_id="owner",
    project_id="p",
    profile_id="worker-codex",
)
SERVICE_ARGS = {
    "wait_source_commit": dict(
        source_key="task:source",
        generation=0,
        revision="r1",
        outcome_ref="task:source:r1",
        payload={"status": "COMPLETED"},
    ),
    "wait_deadline_fire": dict(wait_id="w", deadline_type="transition", generation=1),
    "wait_resume_dispatch": dict(continuation_id="c", generation=1),
    "wait_sleep_decide": dict(wait_id="w"),
    "wait_evict": dict(wait_id="w", reason="resource pressure"),
    "scheduler_wait_sync": dict(project_id="p", blockers=[], cycle_id="cycle-1"),
}


@pytest.fixture
def commands(env):
    orch = SimpleNamespace(
        db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None, wait_v2_handlers={}
    )
    config = AppConfig()
    config.wait_policy = SimpleNamespace(v2_enabled=True, v1_bodies="translate")
    return CommandHandler(orch, config)


async def execute(commands, name, args, principal=WORKER):
    with principal_context(principal):
        return await commands.execute(name, args)


async def supervisor(env, *, fleet=False, project="p"):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == "s")
            .values(
                lifecycle="named",
                profile_id="supervisor",
                project_id=project,
            )
        )
    grants = PUBLIC_AND_EXISTING | ({"waits.fleet_summary"} if fleet else set())
    return replace(
        WORKER,
        elevated=True,
        project_id=project,
        profile_id="supervisor",
        task_id=None,
        policy=CapabilityPolicy.from_namespaces(aq_commands=sorted(grants)),
    )


@pytest.mark.parametrize("name", sorted(SERVICE_WAIT_COMMANDS))
@pytest.mark.parametrize(
    "principal",
    [
        WORKER,
        replace(WORKER, elevated=True),
        TRUSTED_LOCAL,
        ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK, policy=POLICY, project_id="p", parent_run_id="r"
        ),
    ],
)
async def test_service_verbs_reject_other_principals_before_callback(commands, name, principal):
    callback = AsyncMock(return_value={"success": True, "result": {}})
    commands.orchestrator.wait_v2_handlers[name] = callback
    result = await execute(commands, name, SERVICE_ARGS[name], principal)
    assert result["success"] is False
    assert result["error_code"] == "wait.out_of_scope"
    callback.assert_not_awaited()


@pytest.mark.parametrize("name", sorted(SERVICE_WAIT_COMMANDS))
async def test_service_verbs_accept_only_bound_daemon_identity(commands, name):
    callback = AsyncMock(return_value={"success": True, "result": {"committed": True}})
    commands.orchestrator.wait_v2_handlers[name] = callback
    principal = ExecutionPrincipal.service("waits-test")
    result = await execute(commands, name, SERVICE_ARGS[name], principal)
    assert result["success"], result
    assert callback.await_args.kwargs["principal"] is principal
    assert callback.await_args.kwargs["scope"] is None


def test_all_verbs_are_explicitly_contracted_and_service_surfaces_are_absent():
    tool_names = {tool["name"] for tool in _ALL_TOOL_DEFINITIONS}
    for name in PUBLIC_AND_EXISTING | SERVICE_WAIT_COMMANDS | {"wait_legacy_bridge"}:
        assert CONTRACTS.get(name) is not None, name
        assert hasattr(CommandHandler, f"_cmd_{name}")
    assert PUBLIC_AND_EXISTING <= tool_names
    assert SERVICE_WAIT_COMMANDS.isdisjoint(tool_names)
    assert SERVICE_WAIT_COMMANDS <= DEFAULT_EXCLUDED_COMMANDS & API_EXCLUDED & EXCLUDED
    assert SERVICE_WAIT_COMMANDS.isdisjoint(AGENT_COMMAND_SET)
    assert {"wait_explain", "wait_checkpoint", "wait_ack"} <= AGENT_COMMAND_SET
    assert {"wait_graph", "wait_decide", "wait_lift"}.isdisjoint(AGENT_COMMAND_SET)
    assert PUBLIC_AND_EXISTING <= DETAILED_ERROR_COMMANDS
    assert AQ_FEATURE_CAPABILITIES == {"waits.fleet_summary"}
    assert "waits.fleet_summary" not in tool_names


@pytest.mark.parametrize(
    "fields, expected",
    [
        (dict(kind="task", ref="source"), dict(kind="task", task_id="source")),
        (dict(kind="job", ref="j"), dict(kind="job", job_id="j")),
        (dict(kind="timer", due_at=NOW + 300), dict(kind="timer", due_at=NOW + 300)),
        (
            dict(kind="message", ref="reply", after_seq=7),
            dict(kind="message", thread_id="reply", after_seq=7),
        ),
    ],
)
def test_v1_translation_preserves_source_identity(fields, expected):
    values = WaitRegisterArgs.model_validate({**fields, "idempotency_key": "k"})
    assert translate_registration(values) == expected


@pytest.mark.parametrize("version", [1, 2])
async def test_registration_composes_verified_registry_and_preserves_receipt(
    commands, env, version
):
    async def registration(args, *, principal, scope, handler):
        assert scope == env.scope
        # The runtime owner supplies policy/lifecycle composition. The boundary
        # neither invents evidence nor sleeps a session on its own.
        # The writer leaf owns db.wait_source_watermark. Exercise its fence
        # contract on this fixture, whose source log and retention floor are empty.
        async with handler.db._engine.begin() as reader:
            await reader.execute(select(func.pg_advisory_xact_lock(0x415157414954)))
            watermark = await reader.scalar(
                select(func.coalesce(func.max(wait_source_events.c.seq), 0))
            )
        async with handler.db._engine.begin() as conn:
            return {
                "success": True,
                **await register_wait(
                    conn,
                    scope=scope,
                    condition=args["condition"],
                    registry=env.registry,
                    policy=Policy(),
                    decide_sleep=decide,
                    now=NOW,
                    idempotency_key=args["idempotency_key"],
                    source_watermark=watermark,
                ),
            }

    commands.orchestrator.wait_v2_handlers["wait_register"] = registration
    args = dict(schema_version=version, idempotency_key="register", claim_epoch=1, timeout=100)
    args.update(
        dict(kind="task", ref="source")
        if version == 1
        else dict(condition={"kind": "task", "task_id": "source"}, claimed="still running")
    )
    result = await execute(commands, "wait_register", args)
    assert result["success"], result
    assert result["warnings"] == ["timeout_ignored"]
    receipt = WaitResponse.model_validate(result)
    assert receipt.evidence and receipt.decision
    assert receipt.wait_id
    replay = await execute(commands, "wait_register", args)
    assert replay == result
    row = await commands._wait_v2_row(receipt.wait_id)
    assert row["owner_id"] == "owner" and row["claim_epoch"] == 1


async def test_upgrade_refusal_is_explicit_and_has_v2_example(commands):
    commands.config.wait_policy.v1_bodies = "refuse"
    result = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", idempotency_key="upgrade", claim_epoch=1),
    )
    assert result["error_code"] == "wait.upgrade_required"
    assert result["example"]["condition"]["task_id"] == "producer-task"


async def test_explicit_v2_body_requires_condition_and_stays_disabled(commands):
    commands.config.wait_policy.v2_enabled = False
    missing = await execute(commands, "wait_register", dict(schema_version=2, idempotency_key="k"))
    assert missing["error_code"] == "wait.condition_required"
    disabled = await execute(
        commands,
        "wait_register",
        dict(
            schema_version=2,
            condition={"kind": "task", "task_id": "source"},
            idempotency_key="k",
            claim_epoch=1,
        ),
    )
    assert disabled["error_code"] == "wait.disabled"
    assert not getattr(getattr(AppConfig(), "wait_policy", None), "v2_enabled", False)


async def test_missing_runtime_refuses_without_creating_rows(commands, env):
    result = await execute(
        commands,
        "wait_register",
        dict(
            schema_version=2,
            condition={"kind": "task", "task_id": "source"},
            idempotency_key="k",
            claim_epoch=1,
        ),
    )
    assert result["error_code"] == "wait.unavailable"
    assert (await execute(commands, "wait_list", {}))["count"] == 0


@pytest.mark.parametrize(
    "forgery",
    [
        dict(owner_kind="scheduler", owner_id="owner"),
        dict(project_id="q"),
        dict(task_id="foreign"),
        dict(session_id="other"),
    ],
)
async def test_registration_never_trusts_requested_owner_scope(commands, forgery):
    callback = AsyncMock()
    commands.orchestrator.wait_v2_handlers["wait_register"] = callback
    result = await execute(
        commands,
        "wait_register",
        dict(
            schema_version=2,
            condition={"kind": "task", "task_id": "source"},
            idempotency_key="k",
            claim_epoch=1,
            **forgery,
        ),
    )
    assert result["success"] is False
    assert result["error_code"] in {"wait.out_of_scope", "out_of_scope"}
    callback.assert_not_awaited()


async def test_reads_remain_scoped_and_available_while_activation_is_disabled(commands, env):
    receipt = await register(env)
    commands.config.wait_policy.v2_enabled = False
    shown = await execute(commands, "wait_get", dict(wait_id=receipt["wait_id"], schema_version=2))
    assert shown["success"] and shown["wait_id"] == receipt["wait_id"]
    callback = AsyncMock(return_value={"success": True, "explanation": {"evidence": "verified"}})
    commands.orchestrator.wait_v2_handlers["wait_explain"] = callback
    result = await execute(commands, "wait_explain", dict(wait_id=receipt["wait_id"]))
    assert result["success"]
    assert callback.await_args.kwargs["scope"] == dict(
        project_id="p", owner_kind="task", owner_id="owner"
    )
    foreign = await execute(commands, "wait_explain", dict(task_id="foreign"))
    assert foreign["error_code"] == "wait.out_of_scope"
    assert callback.await_count == 1


async def test_read_never_exposes_another_owners_row(commands, env):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(wait_records)
            .where(wait_records.c.id == receipt["wait_id"])
            .values(owner_id="source", claimed="private commentary")
        )
    callback = AsyncMock()
    commands.orchestrator.wait_v2_handlers["wait_explain"] = callback
    result = await execute(commands, "wait_explain", dict(wait_id=receipt["wait_id"]))
    assert result["error_code"] == "wait.out_of_scope" and "private commentary" not in str(result)
    callback.assert_not_awaited()
    assert (await execute(commands, "wait_list", {}))["count"] == 0


async def test_checkpoint_requires_current_holder_before_runtime_mutation(commands, env):
    receipt = await register(env)
    callback = AsyncMock(return_value={"success": True, "continuation_id": "c", "generation": 1})
    commands.orchestrator.wait_v2_handlers["wait_checkpoint"] = callback
    args = dict(wait_id=receipt["wait_id"], summary="Resume the source task.", claim_epoch=0)
    stale = await execute(commands, "wait_checkpoint", args)
    assert stale["error_code"] == "continuation.stale_generation"
    callback.assert_not_awaited()
    result = await execute(commands, "wait_checkpoint", {**args, "claim_epoch": 1})
    assert result["success"] and callback.await_args.kwargs["scope"] == env.scope


@pytest.mark.parametrize("legacy_alias", [False, True])
async def test_show_consume_uses_ack_and_forwards_claim_fence(commands, env, legacy_alias):
    receipt = await register(env)
    read_id = receipt["wait_id"]
    if legacy_alias:
        read_id = "legacy-wait"
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).where(
                wait_records.c.id == receipt["wait_id"],
            ).values(legacy_kind="agent_wait", legacy_id=read_id))
    callback = AsyncMock(
        return_value={"success": True, "wait_id": receipt["wait_id"], "acknowledged": True}
    )
    commands.orchestrator.wait_v2_handlers["wait_ack"] = callback
    result = await execute(
        commands, "wait_get", dict(wait_id=read_id, consume=True, claim_epoch=1)
    )
    assert result["success"] and result["next_step"] == "continue"
    assert callback.await_args.args[0]["claim_epoch"] == 1
    assert callback.await_args.args[0]["wait_id"] == receipt["wait_id"]
    callback.return_value = dict(success=False, error_code="continuation.stale_generation")
    assert (
        await execute(
            commands, "wait_get", dict(wait_id=read_id, consume=True, claim_epoch=1)
        )
    )["error_code"] == "continuation.stale_generation"


async def test_fleet_metadata_requires_explicit_grant_even_for_supervisor(commands, env):
    callback = AsyncMock(return_value={"success": True, "graph": {"nodes": []}})
    commands.orchestrator.wait_v2_handlers["wait_graph"] = callback
    principal = await supervisor(env)
    denied = await execute(commands, "wait_graph", dict(fleet=True), principal)
    assert denied["error_code"] == "capability_denied"
    callback.assert_not_awaited()
    scoped = await execute(commands, "wait_graph", {}, principal)
    assert scoped["success"] and callback.await_args.kwargs["scope"] == {"project_id": "p"}
    principal = await supervisor(env, fleet=True, project=None)
    assert (await execute(commands, "wait_graph", dict(fleet=True), principal))["success"]
    assert callback.await_args.kwargs["scope"] == {"fleet": True}


async def test_supervisor_verbs_check_live_identity_and_project(commands, env):
    callback = AsyncMock(return_value={"success": True, "graph": {}})
    commands.orchestrator.wait_v2_handlers["wait_graph"] = callback
    forged = await execute(
        commands, "wait_graph", dict(project_id="p"), replace(WORKER, elevated=True)
    )
    assert forged["error_code"] == "wait.out_of_scope"
    principal = await supervisor(env)
    foreign = await execute(commands, "wait_graph", dict(project_id="q"), principal)
    assert foreign["error_code"] == "wait.out_of_scope"
    stale = await execute(
        commands,
        "wait_graph",
        dict(project_id="p"),
        replace(principal, session_instance_token="stale"),
    )
    assert stale["error_code"] == "wait.out_of_scope"
    callback.assert_not_awaited()


async def test_named_supervisor_cancel_keeps_project_scope_without_impersonating_holder(
    commands, env
):
    receipt = await register(env)
    principal = await supervisor(env)
    callback = AsyncMock(return_value={"success": True, "wait_id": receipt["wait_id"]})
    commands.orchestrator.wait_v2_handlers["wait_cancel"] = callback
    result = await execute(commands, "wait_cancel", dict(wait_id=receipt["wait_id"]), principal)
    assert result["success"], result
    assert callback.await_args.kwargs["scope"] == {"project_id": "p"}


async def test_forged_principal_and_verified_evidence_never_cross_boundary(commands):
    callback = AsyncMock()
    commands.orchestrator.wait_v2_handlers["wait_source_commit"] = callback
    result = await execute(
        commands,
        "wait_source_commit",
        {
            **SERVICE_ARGS["wait_source_commit"],
            "_principal": ExecutionPrincipal.service("forged"),
        },
    )
    assert result["error_code"] == "wait.out_of_scope"
    callback.assert_not_awaited()
    registration = await execute(
        commands,
        "wait_register",
        dict(
            schema_version=2,
            condition={"kind": "task", "task_id": "source", "_verified": {"generation": 0}},
            idempotency_key="forged",
            claim_epoch=1,
        ),
    )
    assert registration["error_code"] == "wait.condition_required"


async def test_wait_sourced_escalations_require_decision_resolver(commands, monkeypatch):
    monkeypatch.setattr(
        commands,
        "_escalation_for_caller",
        AsyncMock(return_value=({"source_kind": "wait"}, TRUSTED_LOCAL, None)),
    )
    result = await execute(
        commands, "escalation_resolve", dict(escalation_id="e", outcome="resolved"), TRUSTED_LOCAL
    )
    assert result["error_code"] == "wait.decide_required"


async def test_operator_wait_graph_requires_audited_wrapper_and_stays_project_scoped(operator):
    handler, principal = operator
    callback = AsyncMock(return_value={"success": True, "graph": {"nodes": []}})
    handler.orchestrator.wait_v2_handlers = {"wait_graph": callback}
    direct = await operator_execute(operator, "wait_graph", project_id="p")
    assert direct["success"] is False
    callback.assert_not_awaited()
    result = await operator_execute(
        operator, "operator_execute", command="wait_graph", arguments={}
    )
    assert result["success"], result
    assert callback.await_args.kwargs["principal"] is principal
    assert callback.await_args.kwargs["scope"] == {"project_id": "p"}
    assert result["operator_audit_id"] > 0
    foreign = await operator_execute(
        operator, "operator_execute", command="wait_graph", arguments={"project_id": "foreign"}
    )
    fleet = await operator_execute(
        operator, "operator_execute", command="wait_graph", arguments={"fleet": True}
    )
    assert not foreign["success"] and not fleet["success"]
    assert callback.await_count == 1


@pytest.mark.parametrize("task_id", ["owner", "foreign"])
async def test_continuation_target_scope_follows_owning_task(env, task_id):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(continuations).values(
                id="continuation",
                task_id=task_id,
                wait_id=receipt["wait_id"],
                suspension_generation=1,
                facts={},
                branch="aq/test",
                prior_status="RUNNING",
                held_cursor_seq=0,
                resume_state="sleeping",
            )
        )
    error = await target_scope_error(
        "wait_resume_dispatch", {"continuation_id": "continuation"}, "p", db=env.db
    )
    if task_id == "owner":
        assert error is None
    else:
        assert error == (
            "out of scope: wait_resume_dispatch targets another project "
            "(continuation_id belongs to q)"
        )


async def test_operator_lift_requires_exact_preflight_and_wait_project(operator):
    handler, _ = operator
    handler.config.wait_policy = SimpleNamespace(v2_enabled=True)
    callback = AsyncMock(return_value={"success": True, "wait_id": "w", "decision": "lift"})
    handler.orchestrator.wait_v2_handlers = {"wait_lift": callback}
    async with handler.db._engine.begin() as conn:
        receipt = await register_wait(
            conn,
            scope=Scope("p", "integration", "integration-test", principal="service"),
            condition={"kind": "human", "subject": "task_comment", "task_id": "t"},
            idempotency_key="operator-human",
            registry=build_registry(clock=lambda: NOW),
            policy=Policy(),
            decide_sleep=decide,
            now=NOW,
            source_watermark=0,
        )
    args = dict(wait_id=receipt["wait_id"], response="Use the approved setup.")
    callback.return_value["wait_id"] = receipt["wait_id"]
    plan = dict(command="wait_lift", arguments=args)
    missing = await operator_execute(operator, "operator_execute", **plan)
    assert not missing["success"] and "preflight" in missing["error"]
    preflight = await operator_execute(operator, "operator_preflight", actions=[plan])
    assert preflight["ready"], preflight
    result = await operator_execute(
        operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"]
    )
    assert result["success"], result
    assert callback.await_args.kwargs["scope"] == {"project_id": "p"}
    async with handler.db._engine.begin() as conn:
        await conn.execute(
            update(wait_records)
            .where(wait_records.c.id == receipt["wait_id"])
            .values(project_id="foreign")
        )
    rejected = await operator_execute(
        operator, "operator_execute", **plan, preflight_id=preflight["preflight_id"]
    )
    assert not rejected["success"] and callback.await_count == 1


def test_playbooks_cannot_decide_or_lift():
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK, policy=POLICY, project_id="p", parent_run_id="r"
    )
    assert caller_allowed("wait_explain", principal)
    assert not caller_allowed("wait_decide", principal)
    assert not caller_allowed("wait_lift", principal)


@pytest.mark.parametrize("summary", [" ", "é" * 4097])
def test_checkpoint_utf8_budget_survives_api_model_boundary(summary):
    assert REQUEST_MODELS["wait_checkpoint"] is WaitCheckpointArgs
    with pytest.raises(ValidationError):
        REQUEST_MODELS["wait_checkpoint"](wait_id="w", summary=summary)


def test_strict_decisions_reject_irrelevant_fields_and_empty_notes():
    for args in [
        dict(decision="wake", note="okay", extend_seconds=60),
        dict(decision="reroute", note="move"),
        dict(decision="cancel", note=" "),
        dict(decision="extend", note="later", extend_seconds=float("inf")),
    ]:
        with pytest.raises(ValidationError):
            WaitDecideArgs(wait_id="w", **args)
    with pytest.raises(ValidationError):
        WAIT_V2_ARGS["wait_ack"](wait_id="w", generation=1, principal="service")


@pytest.mark.parametrize(
    "profile_id", ["worker-codex", "worker-claude", "worker-opencode", "supervisor"]
)
async def test_copied_vault_additive_sync_closes_wait_grant_drift(tmp_path, profile_id):
    path = Path(vault_profile_path(str(tmp_path), profile_id))
    path.parent.mkdir(parents=True)
    shipped = Path(shipped_profile_path(profile_id)).read_text()
    grants = (
        {"wait_explain", "wait_checkpoint", "wait_ack"}
        if profile_id.startswith("worker")
        else {
            "wait_get",
            "wait_list",
            "wait_explain",
            "wait_graph",
            "wait_decide",
            "wait_lift",
            "waits.fleet_summary",
        }
    )
    # A copied old vault profile exercises the same startup merge without
    # restarting the operator daemon from a worker slot.
    old = shipped
    for grant in grants:
        old = old.replace(f'    "{grant}",\n', "")
    path.write_text(old)
    before = diff_profile(profile_id, str(tmp_path))
    assert before.missing_grants
    result = sync_profile_capabilities(str(tmp_path), profile_id)
    assert result.changed and grants <= set(result.added_names())
    assert not diff_profile(profile_id, str(tmp_path)).missing_grants
    assert sync_profile_capabilities(str(tmp_path), profile_id).status == "current"
    # Invoke the actual read-only doctor check against the copied vault after
    # the startup sync, rather than managing the operator daemon in this slot.
    import importlib
    from src.doctor.models import Severity

    check = importlib.import_module("src.doctor.profile_checks")._check_system_profile_drift
    result = await check(SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    assert result.severity == Severity.OK, result.detail


@pytest.fixture
def cli_transport(monkeypatch):
    import src.cli.waits as mod

    calls = []

    def transport(ctx, name, params):
        calls.append((name, params))
        return dict(success=True)

    monkeypatch.setattr(mod, "_execute", transport)
    monkeypatch.setattr(mod, "resolve_claim_epoch", lambda value: value or 17)
    return mod.wait, calls


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--task", "source"], dict(kind="task", task_id="source")),
        (
            ["--kind", "message", "--ref", "reply", "--after-seq", "7", "--schema-version", "2"],
            dict(kind="message", thread_id="reply", after_seq=7),
        ),
        (
            [
                "--kind",
                "ci_check",
                "--sha",
                "a" * 40,
                "--repository",
                "repo",
                "--check-name",
                "unit",
            ],
            dict(kind="ci_check", head_sha="a" * 40, repository_id="repo", check_name="unit"),
        ),
    ],
)
def test_cli_typed_registration_preserves_exact_source_and_epoch(cli_transport, flags, expected):
    group, calls = cli_transport
    result = CliRunner().invoke(group, ["register", *flags, "--idempotency-key", "k"])
    assert result.exit_code == 0, result.output
    name, params = calls.pop()
    assert name == "wait_register" and params["schema_version"] == 2
    assert params["condition"] == expected and params["claim_epoch"] == 17
    assert not {"ref", "after_seq", "due_at"} & params.keys()


@pytest.mark.parametrize(
    "flags",
    [
        ["--kind", "message", "--ref", "reply"],
        ["--condition", "[]"],
        ["--condition", '{"kind":"task","task_id":"t"}', "--task", "t"],
        ["--schema-version", "1", "--task", "t"],
    ],
)
def test_cli_invalid_registration_never_reaches_transport(cli_transport, flags):
    group, calls = cli_transport
    result = CliRunner().invoke(group, ["register", *flags, "--idempotency-key", "k"])
    assert result.exit_code != 0 and not calls


def test_cli_checkpoint_reads_summary_file_and_sends_epoch(cli_transport, tmp_path):
    group, calls = cli_transport
    summary = tmp_path / "resume.txt"
    summary.write_text("Continue with source verification.")
    result = CliRunner().invoke(
        group, ["checkpoint", "--wait-id", "w", "--summary-file", str(summary)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ("wait_checkpoint", dict(wait_id="w", summary=summary.read_text(), claim_epoch=17))
    ]


def test_cli_explain_labels_verified_evidence_before_commentary(cli_transport, monkeypatch):
    import src.cli.waits as mod

    group, _ = cli_transport
    monkeypatch.setattr(
        mod,
        "_execute",
        lambda *args: dict(
            success=True,
            explanation=dict(evidence={"source_revision": "r1"}, claimed="agent says almost ready"),
        ),
    )
    result = CliRunner().invoke(group, ["explain", "w"])
    assert result.exit_code == 0, result.output
    assert result.output.index("Verified evidence:") < result.output.index("Agent commentary:")
    assert "r1" in result.output


def test_cli_public_verbs_have_inventory_backend_evidence():
    from src.cli.inventory import _infer_handwritten_backend
    from src.cli.waits import wait

    expected = {
        "register": "wait_register",
        "show": "wait_get",
        "list": "wait_list",
        "cancel": "wait_cancel",
        "explain": "wait_explain",
        "graph": "wait_graph",
        "checkpoint": "wait_checkpoint",
        "ack": "wait_ack",
        "decide": "wait_decide",
        "lift": "wait_lift",
        "bridge": "wait_legacy_bridge",
    }
    for path, name in expected.items():
        assert _infer_handwritten_backend(wait.commands[path]) == name


async def human_runtime(env, subject="task_comment"):
    config = AppConfig()
    config.wait_policy.v2_enabled = config.wait_policy.hibernation_enabled = True
    orch = SimpleNamespace(db=env.db, config=config, wait_registry=env.registry,
                           bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    service = WaitService(orch, clock=lambda: NOW)
    handler = CommandHandler(orch, config)
    orch._command_handler = handler
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            branch_name="aq/owner"))
        if subject == "review":
            await conn.execute(insert(doc_reviews).values(
                id="human-review", project_id="p", author_task_id="owner", kind="spec",
                title="Proposal", vault_path="specs/human.md", current_revision=1,
                state="in_review", created_at=NOW, updated_at=NOW))
    if subject == "message_thread":
        question = await env.db.create_message(
            project_id="p", from_kind="session", from_id="s", to_kind="user", to_id="dashboard",
            thread_id="human-thread", body="May I proceed?")
        condition = dict(kind="human", subject=subject, thread_id="human-thread",
                         after_seq=question.created_seq)
    elif subject == "review":
        condition = dict(kind="human", subject=subject, review_id="human-review")
    else:
        condition = dict(kind="human", subject=subject, task_id="owner")
    receipt = await execute(handler, "wait_register", dict(
        schema_version=2, condition=condition, idempotency_key="human-runtime", claim_epoch=1))
    assert receipt["success"], receipt
    await env.db.create_session(SessionRecord(
        id="supervisor", project_id="p", profile_id="supervisor", harness="codex",
        provider="fake", name="supervisor", lifecycle="named", state="running",
        desired_state="running", instance_token="supervisor-token", epoch="boot",
        work_dir="/tmp/supervisor", started_at=NOW))
    principal = replace(WORKER, session_id="supervisor", session_instance_token="supervisor-token",
                        task_id=None, profile_id="supervisor", elevated=True)
    async with env.db._engine.begin() as conn:
        await env.db.park_task_for_continuation(
            conn, task_id="owner", wait_id=receipt["wait_id"],
            continuation_id=receipt["continuation_id"], generation=receipt["generation"],
            session_id="s", instance_token="token", claim_epoch=1,
            saved_head="a" * 40, now=NOW + 10)
    return service, handler, principal, receipt


@pytest.mark.parametrize("subject", ["review", "task_comment", "message_thread"])
async def test_human_lift_commits_response_and_one_fresh_resume(env, subject):
    service, handler, principal, receipt = await human_runtime(env, subject)
    service.human.clock = lambda: NOW + 86400 * 4
    result = await execute(handler, "wait_lift", dict(
        wait_id=receipt["wait_id"], response="Proceed with the approved scope.",
        expected_generation=receipt["generation"]), principal)
    assert result["success"], result
    assert result["outcome"] == "lifted_by_supervisor"
    assert WaitDecideResponse.model_validate(result).decision == "lift"
    async with env.db._engine.connect() as conn:
        wait = dict((await conn.execute(select(wait_records))).mappings().one())
        intent = (await conn.execute(select(continuations))).mappings().one()
        answer = (await conn.execute(select(messages).where(
            messages.c.body_kind == "wait_lift"))).mappings().one()
        audit = (await conn.execute(select(events).where(
            events.c.event_type == "wait.lifted"))).mappings().all()
        assert not list((await conn.execute(select(wait_deadlines).where(
            wait_deadlines.c.deadline_type.in_(("due", "ceiling", "escalation"))))).mappings())
    assert intent["resume_state"] == "queued"
    assert (await env.db.get_task("owner")).status.value == "RESUME_PENDING"
    assert answer["from_kind"] == "session" and answer["from_id"] == "supervisor"
    assert answer["to_kind"] == "task" and answer["to_id"] == "owner"
    assert answer["body"] == "Proceed with the approved scope."
    envelope = wait_outcome_envelope(wait, intent)
    assert envelope["decision"] == "lift"
    assert envelope["facts"]["supervisor_session_id"] == "supervisor"
    assert envelope["result_ref"] == f"message:{answer['id']}"
    assert len(audit) == 1
    payload = json.loads(audit[0]["payload"])
    assert payload["project_id"] == "p"
    assert payload["supervisor_session_id"] == "supervisor"
    assert payload["message_id"] == answer["id"]
    repeated = await execute(handler, "wait_lift", dict(
        wait_id=wait["id"], response="A second answer"), principal)
    assert repeated["error_code"] == "wait.already_satisfied"
    # A late answer remains ordinary mail and cannot enqueue a second resume.
    late = await env.db.create_message(
        project_id="p", from_kind="user", from_id="dashboard", to_kind="task", to_id="owner",
        thread_id=answer["thread_id"], body="Late human answer")
    async with env.db._engine.connect() as conn:
        event = dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == f"message:{answer['thread_id']}",
            wait_source_events.c.source_revision == str(late.created_seq),
        ))).mappings().one())
    async with env.db._engine.begin() as conn:
        assert not (await service.dispatcher.commit(conn, wait, event))["committed"]
    assert late.delivered_at is None


async def test_parked_human_comment_survives_epoch_fence_and_ignores_supervisor(env):
    service, handler, principal, receipt = await human_runtime(env)
    await env.db.add_task_comment("owner", "Discussing", author_kind="supervisor", author_id="sup")
    async with env.db._engine.connect() as conn:
        wait = dict((await conn.execute(select(wait_records))).mappings().one())
        assert (await service.registry.observe(conn, Condition.from_record(wait["condition"]))).state == "unsatisfied"
    answer = await env.db.add_task_comment("owner", "Approved", author_kind="user", author_id="ui")
    async with env.db._engine.connect() as conn:
        event = dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == wait["source_key"],
            wait_source_events.c.source_generation == wait["source_generation"],
            wait_source_events.c.source_revision == answer["id"],
        ))).mappings().one())
    async with env.db._engine.begin() as conn:
        result = await service.dispatcher.commit(conn, wait, event)
    assert result["outcome"] == "satisfied", result
    assert (await env.db.get_task("owner")).status.value == "RESUME_PENDING"


@pytest.mark.parametrize("change", ["instance", "epoch", "generation", "answer"])
async def test_human_lift_rechecks_identity_and_racing_answer_without_message(env, change):
    _, handler, principal, receipt = await human_runtime(env)
    if change == "instance":
        await env.db.update_session("supervisor", instance_token="replacement")
    elif change == "epoch":
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
                assigned_agent_id="a"))
    elif change == "answer":
        await env.db.add_task_comment("owner", "User wins", author_kind="user", author_id="ui")
    args = dict(wait_id=receipt["wait_id"], response="Supervisor answer")
    if change == "generation":
        args["expected_generation"] = 99
    result = await execute(handler, "wait_lift", args, principal)
    assert not result["success"], result
    async with env.db._engine.connect() as conn:
        assert not await conn.scalar(select(messages.c.id).where(messages.c.body_kind == "wait_lift"))
        assert await conn.scalar(select(wait_records.c.outcome)) == "pending"


async def test_human_registration_requires_hibernation_before_any_wait_write(env):
    from src.wait_runtime.lifecycle import WaitLifecycle

    config = AppConfig()
    config.wait_policy.v2_enabled = True
    lifecycle = WaitLifecycle(SimpleNamespace(orch=SimpleNamespace(db=env.db, config=config)))
    with pytest.raises(WaitError) as error:
        await lifecycle.register(dict(condition=dict(kind="human", subject="task_comment",
                                                      task_id="owner")),
                                 principal=WORKER, scope=env.scope, handler=None)
    assert error.value.code == "wait.disabled"
    async with env.db._engine.connect() as conn:
        assert not await conn.scalar(select(wait_records.c.id))
