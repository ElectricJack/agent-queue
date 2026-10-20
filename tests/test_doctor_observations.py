"""Scope-first observation, producer completeness and stable identities."""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.command_inventory import command_snapshot
from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler
from src.commands.principal import (
    ExecutionPrincipal, PrincipalKind, current_principal, principal_context,
)
from src.config import AppConfig
from src.database import Database
from src.doctor.models import (
    CheckResult, DoctorCheck, DoctorContext, ObservationUnavailable, Severity,
    observation_context, observation_read_failed,
)
from src.doctor.observations import (
    GROUP, GROUPS, bounded_inline, catalog, collect_observations, finding, normalize, partition,
)
from src.doctor.pool_checks import CHECKS
from src.doctor.runner import DoctorRegistry, _run_one
from src.models import Agent, Project, Task, TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn

stall = importlib.import_module("src.doctor.stall_checks")
HOLDER = next(check for check in CHECKS if check.id == "claims.holder_consistency")
NOW = 1_800_000_000.0


@pytest.fixture
async def db():
    database = Database(lease_dsn("doctor-observations"))
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def registry(*checks):
    result = DoctorRegistry()
    for check in checks:
        result.register(check)
    return result


def holder_db(count=55, project="p"):
    return SimpleNamespace(
        list_tasks=AsyncMock(return_value=[SimpleNamespace(
            id=f"t{i}", project_id=project, assigned_agent_id="a") for i in range(count)]),
        list_sessions=AsyncMock(return_value=[]),
        list_projects=AsyncMock(return_value=[SimpleNamespace(id="p"), SimpleNamespace(id="empty")]),
        get_project=AsyncMock(return_value=SimpleNamespace(id=project)),
        get_agent=AsyncMock(return_value=None), get_task_meta=AsyncMock(return_value=None),
    )


def handler(db, *checks):
    config = AppConfig()
    config.security.capability_enforcement = "enforce"
    return CommandHandler(SimpleNamespace(
        db=db, plugin_registry=None, bus=SimpleNamespace(emit=AsyncMock()),
        provider_availability=object(),
    ), config, registry(*checks))


def principal(project=None, commands=("doctor_run",), kind=PrincipalKind.PLAYBOOK):
    return ExecutionPrincipal(kind=kind, project_id=project, elevated=True,
                              policy=CapabilityPolicy.from_namespaces(aq_commands=commands))


async def test_full_holder_producer_keeps_item_51_and_legacy_data():
    db = holder_db()
    response = await handler(db, HOLDER).execute("doctor_run", {"check_id": HOLDER.id, "now": NOW})
    assert response["success"] and response["complete"]
    assert response["observed_at"] == NOW and response["exit_code"] == 1
    assert len(response["findings"]) == 55
    assert response["checks"][0]["data"] == {
        "count": 55, "tasks": [f"t{i}" for i in range(50)],
    }
    assert response["findings"][50]["subject"] == {"type": "task", "id": "t50"}
    assert all(f["project_id"] == "p" for f in response["findings"])
    assert any(p["project_id"] == "empty" and p["complete"] for p in response["coverage"])
    db.list_tasks.assert_awaited_once()  # no follow-up query to recover truncated IDs


async def test_real_postgres_holder_inventory_over_50(db):
    await db.create_project(Project(id="obs", name="Observations"))
    await db.create_agent(Agent(id="obs-agent", name="obs-agent", profile_id="worker"))
    for index in range(52):
        await db.create_task(Task(id=f"obs-{index}", project_id="obs", title="holder",
                                  description="", status=TaskStatus.IN_PROGRESS,
                                  assigned_agent_id="obs-agent"))
    response = await handler(db, HOLDER).execute("doctor_run", {"check_id": HOLDER.id})
    assert response["success"] and response["complete"]
    assert {f["subject"]["id"] for f in response["findings"]} == {f"obs-{i}" for i in range(52)}
    assert {f["project_id"] for f in response["findings"]} == {"obs"}


@pytest.mark.parametrize("project", ["p", "other"])
@pytest.mark.parametrize("kind", [PrincipalKind.PLAYBOOK, PrincipalKind.SESSION])
async def test_project_principal_cannot_scan_global_even_with_grant(project, kind):
    db = holder_db()
    h = handler(db, HOLDER)
    with principal_context(principal(project, kind=kind)):
        result = await h.execute("doctor_run", {"check_id": HOLDER.id})
    assert result["success"] is False
    assert result["error_code"] == "resource_scope_denied"
    db.list_tasks.assert_not_awaited()
    db.list_sessions.assert_not_awaited()


async def test_system_principal_cannot_request_project_filter_or_fix():
    db = holder_db()
    h = handler(db, HOLDER)
    with principal_context(principal()):
        response = await h.execute("doctor_run", {"check_id": HOLDER.id, "project_id": "p"})
        fixed = await h.execute("doctor_run", {"check_id": HOLDER.id, "fix": True})
    assert response["error_code"] == "doctor.unsupported_scope"
    assert fixed["error_code"] == "doctor.invalid_arguments"
    db.list_tasks.assert_not_awaited()


@pytest.mark.parametrize("args", [
    {}, {"check_id": HOLDER.id, "group": GROUP}, {"check_id": HOLDER.id, "fix": False},
    {"check_id": HOLDER.id, "unknown": 1}, {"check_id": HOLDER.id, "now": "123"},
    {"check_id": HOLDER.id, "now": True}, {"check_id": HOLDER.id, "now": float("nan")},
    {"check_id": HOLDER.id, "timeout_s": float("inf")},
    {"check_id": HOLDER.id, "timeout_s": 61}, {"check_id": HOLDER.id, "timeout_s": 0},
    {"check_id": HOLDER.id, "page_token": "x"}, {"check_id": HOLDER.id, "page_size": 501},
])
async def test_strict_arguments_precede_invocation(args):
    db = holder_db()
    assert (await handler(db, HOLDER).execute("doctor_run", args))["success"] is False
    db.list_tasks.assert_not_awaited()


async def test_unknown_empty_and_unavailable_are_never_successful_empty_scans(monkeypatch):
    h = handler(holder_db(), HOLDER)
    for args, code in [({"check_id": "unknown"}, "doctor.unknown_check"),
                       ({"group": "unknown"}, "doctor.unknown_group"),
                       ({"group": GROUP}, "doctor.subsystem_unavailable")]:
        result = await h.execute("doctor_run", args)
        assert not result["success"] and not result["complete"]
        assert result["error_code"] == code
    monkeypatch.setitem(GROUPS, "empty_v1", ())
    assert (await h.execute("doctor_run", {"group": "empty_v1"}))["error_code"] == "doctor.empty_group"
    h._doctor_registry = None
    assert not (await h.execute("doctor_catalog", {}))["success"]


async def test_snapshot_arguments_never_fall_back_to_live_scan():
    db = holder_db()
    response = await handler(db, HOLDER).execute("doctor_run", {
        "check_id": HOLDER.id, "pagination": "snapshot", "snapshot_id": "saved", "page_token": "x",
    })
    assert response["error_code"] == "snapshot.unavailable"
    db.list_tasks.assert_not_awaited()


async def test_project_session_can_discover_catalogue_without_scope_argument_injection():
    args = {}
    scope = RequestScope(kind="session", project_id="p", elevated=True)
    assert check_command_scope("doctor_catalog", args, scope) is None and args == {}
    with principal_context(principal("p", commands=("doctor_catalog",), kind=PrincipalKind.SESSION)):
        result = await handler(holder_db(), HOLDER).execute("doctor_catalog", args)
    assert result["success"]
    assert result["checks"][0]["supported_scopes"] == ["system"]


def test_catalogue_publishes_versions_and_unported_diagnostics():
    legacy = DoctorCheck("legacy", AsyncMock())
    result = catalog(registry(HOLDER, *stall.stall_checks(), legacy))
    assert result["groups"][GROUP] == [HOLDER.id, stall.CHECK_ID]
    checks = {c["id"]: c for c in result["checks"]}
    assert checks[HOLDER.id]["supported_scopes"] == ["system"]
    assert checks[HOLDER.id]["identity_version"] == checks[HOLDER.id]["payload_version"] == 1
    assert not checks["legacy"]["supported"] and checks["legacy"]["diagnostics"]
    assert set(checks[stall.CHECK_ID]["finding_schema"]["required_task_kinds"]) == {
        "unclaimed_work", "undelivered_blocker", "restore_branch", "worker_relaunch_loop",
    }
    snapshot = command_snapshot()
    for name in ("doctor_run", "doctor_catalog"):
        assert snapshot.commands[name].effect_class == "read"
        assert snapshot.commands[name].schema_source == "fallback"


async def test_unported_check_not_invoked():
    run = AsyncMock()
    result = await handler(None, DoctorCheck("legacy", run)).execute("doctor_run", {"check_id": "legacy"})
    assert result["error_code"] == "doctor.unsupported_observation"
    run.assert_not_awaited()


@pytest.mark.parametrize(("behavior", "status"), [
    ("timeout", "timeout"), ("exception", "exception"), ("unavailable", "unavailable"),
    ("wrong_type", "malformed"), ("severity", "malformed"), ("non_json", "malformed"),
    ("coverage", "malformed"),
])
async def test_runner_execution_failures_are_explicit(behavior, status):
    async def run(ctx):
        if behavior == "timeout":
            await asyncio.sleep(5)
        if behavior == "exception":
            raise RuntimeError("arbitrary wording")
        if behavior == "unavailable":
            raise ObservationUnavailable("arbitrary wording")
        if behavior == "wrong_type":
            return {}
        return CheckResult("x", "invalid" if behavior == "severity" else Severity.OK, "text",
                           coverage=[None] if behavior == "coverage" else [],
                           data={"bad": float("nan")} if behavior == "non_json" else {})
    result = await _run_one(DoctorCheck("x", run, timeout_s=.01), DoctorContext(AppConfig()))
    assert result.execution_status == status
    assert not result.complete and result.reasons


async def test_health_text_never_determines_execution_success():
    run = AsyncMock(return_value=CheckResult("x", Severity.ERROR, "timeout failure unavailable",
        execution_status="succeeded", complete=True, coverage=[
            partition("x", "aggregate", None, complete=True)]))
    check = DoctorCheck("x", run, supported_scopes=("system",), read_only_audited=True,
                        finding_schema={"kinds": ["aggregate"]},
                        identity_adapter=lambda r: [], completeness_contract="complete")
    response = await collect_observations(registry(check), DoctorContext(AppConfig()), check_id="x")
    assert response["success"] and response["complete"] and response["exit_code"] == 2
    assert CheckResult("old", Severity.OK, "healthy").complete is False


async def test_missing_project_identity_keeps_other_findings_and_invalidates_partition():
    db = holder_db(2)
    db.list_tasks.return_value[1].project_id = None
    response = await handler(db, HOLDER).execute("doctor_run", {"check_id": HOLDER.id})
    assert len(response["findings"]) == 1 and not response["complete"]
    assert response["success"] is False
    assert all(not part["complete"] for part in response["coverage"])
    assert "finding_identity_missing_or_invalid" in response["reasons"]


@pytest.mark.parametrize("kind", ["undeclared", {"malformed": "kind"}])
async def test_unattributable_identity_cannot_leave_any_partition_complete(kind):
    run = AsyncMock(return_value=CheckResult("x", Severity.OK, "observed",
        execution_status="succeeded", complete=True, coverage=[
            partition("x", "aggregate", "p", complete=True)]))
    check = DoctorCheck("x", run, supported_scopes=("system",), read_only_audited=True,
                        finding_schema={"kinds": ["aggregate"]},
                        identity_adapter=lambda r: [finding(
                            "x", kind, "p", "task", "t", "observed", {})],
                        completeness_contract="complete")
    response = await collect_observations(registry(check), DoctorContext(AppConfig()), check_id="x")
    assert not response["success"] and not response["complete"]
    assert response["findings"] == []
    assert response["checks"][0]["execution_status"] == "malformed"
    assert all(not part["complete"] for part in response["coverage"])
    assert response["checks"][0]["coverage"] == response["coverage"]


async def test_holder_budget_is_explicit(monkeypatch):
    module = importlib.import_module("src.doctor.observations")
    monkeypatch.setattr(module, "MAX_ROWS", 2)
    response = await handler(holder_db(4), HOLDER).execute("doctor_run", {"check_id": HOLDER.id})
    assert not response["complete"] and not response["success"]
    assert len(response["findings"]) == 2
    part = next(p for p in response["coverage"] if p["project_id"] == "p")
    assert (part["observed"], part["total"]) == (2, 4)
    assert "producer_limit" in part["reasons"]


def test_keys_ignore_wording_age_order_and_time_but_include_partition():
    check = stall.stall_checks()[0]
    a = finding(check.id, "unclaimed_work", "p", "task", "t", "old", {"age": 1})
    original = normalize(a, check)["key"]
    assert normalize(dict(a, detail="new", data={"age": 200, "time": NOW}), check)["key"] == original
    for change in ({"project_id": "q"}, {"kind": "undelivered_blocker"},
                   {"subject": {"type": "task", "id": "other"}}):
        assert normalize(dict(a, **change), check)["key"] != original


def test_multiple_blockers_share_one_subject_identity_and_preserve_evidence():
    rows = [stall._finding("restore_branch", "p", f"blocked by {task}",
                          dependent_task_id="dependent", blocker_task_id=task)
            for task in ("a", "b")]
    check = stall.stall_checks()[0]
    def adapt(items):
        return [normalize(item, check) for item in stall._stall_observations(
            CheckResult(stall.CHECK_ID, Severity.WARN, "", observation_data=items))]
    assert adapt(rows) == adapt(list(reversed(rows)))
    assert len(adapt(rows)) == 1
    assert len(adapt(rows)[0]["data"]["evidence"]) == 2


@pytest.fixture
def sweep(monkeypatch):
    # Suppress unrelated real-machine probes; required task probes remain real.
    for name in ("_flock_findings", "_conversation_findings", "_container_dependency_findings",
                 "_branch_findings", "_session_findings", "_delivery_findings", "_provider_findings",
                 "_unmaterialized_pr_findings", "_completed_undelivered_findings",
                 "_unadmitted_parent_findings", "_stranded_child_findings",
                 "_reviewed_file_guard_findings", "_orphaned_pr_findings", "_validation_findings",
                 "_subject_unknown_findings", "_object_bootstrap_findings",
                 "_continuous_promotion_findings", "_escalation_delivery_findings",
                 "_slot_capacity_findings", "_train_batch_findings"):
        monkeypatch.setattr(stall, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(stall, "_waiting_train_repairs", AsyncMock(return_value={}))
    monkeypatch.setattr(stall, "_log_findings", lambda *args: [])
    monkeypatch.setattr(importlib.import_module("src.integration.delivery_engine"),
                        "project_delivery_issues", AsyncMock(return_value=[]))
    tasks = [SimpleNamespace(id="t", project_id="p", status=TaskStatus.READY,
                             updated_at=NOW-3000, profile_id="worker", title="t", task_type=None)]
    db = SimpleNamespace(list_projects=AsyncMock(return_value=[SimpleNamespace(id="p")]),
                         list_tasks=AsyncMock(return_value=tasks),
                         get_task_meta_bulk=AsyncMock(return_value={"t": {"count": 3}}))
    h = handler(db, *stall.stall_checks())
    h._cmd_explain_task = AsyncMock(return_value={"success": True, "reasons": []})
    return h


async def test_required_kinds_have_independent_coverage_and_frozen_time(sweep):
    response = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    assert response["success"] and response["complete"]
    assert {f["kind"] for f in response["findings"]} == {"unclaimed_work", "worker_relaunch_loop"}
    assert all(p["complete"] for p in response["coverage"])
    assert set(stall.REQUIRED_TASK_KINDS) <= {p["kind"] for p in response["coverage"]}


async def test_nested_dispatch_grant_never_widens_and_independent_partition_survives(sweep):
    caller = principal()
    with principal_context(caller):
        result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    sweep._cmd_explain_task.assert_not_awaited()
    coverage = {p["kind"]: p for p in result["coverage"]}
    assert not result["success"]
    assert not coverage["unclaimed_work"]["complete"]
    assert not coverage["undelivered_blocker"]["complete"]
    assert coverage["worker_relaunch_loop"]["complete"]
    assert [f["kind"] for f in result["findings"]] == ["worker_relaunch_loop"]


async def test_nested_dispatch_keeps_principal_and_caught_read_failures(sweep):
    seen = []
    async def explain(args):
        seen.append(current_principal())
        observation_read_failed("blocking_dependencies_unavailable", "p")
        return {"success": True, "reasons": []}
    sweep._cmd_explain_task = explain
    caller = principal(commands=("doctor_run", "explain_task"))
    with principal_context(caller):
        result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    assert seen == [caller]
    assert "blocking_dependencies_unavailable" in result["reasons"]
    assert not result["complete"]


async def test_real_explanation_swallowed_read_does_not_claim_complete(db, monkeypatch):
    await db.create_project(Project(id="p", name="p"))
    await db.create_task(Task(id="t", project_id="p", title="t", description=""))
    h = handler(db)
    h.orchestrator.provider_availability = SimpleNamespace(hold_for=AsyncMock(return_value=None))
    monkeypatch.setattr(h, "_development_integration", lambda: None)
    monkeypatch.setattr(h, "_assignment_route_state", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(h, "_pool_wait_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(db, "get_task_labels", AsyncMock(side_effect=RuntimeError("failed read")))
    ctx = DoctorContext(AppConfig(), read_only=True)
    token = observation_context.set(ctx)
    try:
        result = await h.execute("explain_task", {"task_id": "t"})
    finally:
        observation_context.reset(token)
    assert result["success"]
    assert {"reason": "task_labels_unavailable", "project_id": "p"} in ctx.observation_reasons


async def test_missing_provider_context_is_not_healthy_absence(sweep):
    sweep.orchestrator.provider_availability = None
    result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    assert "provider_availability_unavailable" in result["reasons"]
    assert not next(p for p in result["coverage"] if p["kind"] == "unclaimed_work")["complete"]


@pytest.mark.parametrize("code", ["delivery_evidence_unavailable", "development_dependency_delivery"])
async def test_unavailable_admission_evidence_never_authorizes_absence(sweep, code):
    sweep._cmd_explain_task.return_value = {"success": True, "reasons": [{"code": code}]}
    result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    assert "task_explanation_incomplete" in result["reasons"]
    assert not next(p for p in result["coverage"] if p["kind"] == "unclaimed_work")["complete"]
    assert next(p for p in result["coverage"] if p["kind"] == "worker_relaunch_loop")["complete"]


async def test_missing_project_context_is_unavailable(sweep):
    sweep.db.list_projects.return_value = None
    result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID})
    assert not result["success"] and not result["complete"]
    assert result["checks"][0]["execution_status"] == "unavailable"


async def test_known_empty_active_project_inventory_is_complete(sweep):
    sweep.db.list_projects.return_value = []
    result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID})
    assert result["success"] and result["complete"] and result["findings"] == []
    assert {p["kind"] for p in result["coverage"]} == set(stall._SUBJECTS)


async def test_one_failed_project_does_not_clear_or_erase_other_project(sweep):
    sweep.db.list_projects.return_value.append(SimpleNamespace(id="other"))
    tasks = sweep.db.list_tasks.return_value
    async def inventory(project_id):
        if project_id == "other":
            raise OSError("read failed")
        return tasks
    sweep.db.list_tasks.side_effect = inventory
    result = await sweep.execute("doctor_run", {"check_id": stall.CHECK_ID, "now": NOW})
    assert len(result["findings"]) == 2
    parts = [p for p in result["coverage"] if p["kind"] == "worker_relaunch_loop"]
    assert {p["project_id"]: p["complete"] for p in parts} == {"p": True, "other": False}


async def test_restore_branch_identity_and_unknown_evidence_are_independent(monkeypatch):
    async def candidates(ctx, **kwargs):
        ctx.incomplete("repository_evidence_unknown", project_id="p")
        return [{"project_id": "p", "dependent_task_id": "dependent",
                 "blocker_task_id": "blocker", "source_sha": "abc"}]
    monkeypatch.setattr(stall, "_find_stranded_dependents", candidates)
    explains = SimpleNamespace(get=AsyncMock(return_value={"reasons": [
        {"code": "blocked_dependency", "ref": "blocker"}]}))
    ctx = DoctorContext(AppConfig(), read_only=True)
    probe = await stall._probe(ctx, {"p", "q"}, ("restore_branch",),
                               lambda c: stall._branch_findings(c, {"p", "q"}, explains))
    assert {p["project_id"]: p["complete"] for p in probe.coverage} == {"p": False, "q": True}
    result = CheckResult(stall.CHECK_ID, Severity.WARN, "", observation_data=probe.findings)
    item = normalize(stall._stall_observations(result)[0], stall.stall_checks()[0])
    assert item["subject"] == {"type": "task", "id": "dependent"} and item["project_id"] == "p"


async def test_swallowed_repository_timeout_keeps_incomplete_coverage(monkeypatch):
    async def read(*args, **kwargs):
        await asyncio.sleep(1)
    monkeypatch.setattr(importlib.import_module("src.doctor.integration_checks"),
                        "completed_undelivered", read)
    monkeypatch.setattr(stall, "_COMPLETED_UNDELIVERED_BUDGET_SECONDS", .01)
    ctx = DoctorContext(AppConfig(), read_only=True)
    probe = await stall._probe(ctx, {"p"}, ("completed_undelivered",),
                               lambda c: stall._completed_undelivered_findings(c, {"p"}, NOW))
    assert not probe.findings and not probe.coverage[0]["complete"]
    assert probe.coverage[0]["reasons"] == ["repository_probe_timeout"]


async def test_legacy_probe_exception_still_reaches_doctor_health_exit_code():
    async def broken(ctx):
        raise RuntimeError("unreadable")
    async def run(ctx):
        return await stall._probe(ctx, {"p"}, ("restore_branch",), broken)
    legacy = await _run_one(DoctorCheck("x", run), DoctorContext(AppConfig()))
    assert legacy.severity is Severity.ERROR and legacy.execution_status == "exception"
    observed = await run(DoctorContext(AppConfig(), read_only=True))
    assert observed.findings == [] and not observed.coverage[0]["complete"]
    assert observed.coverage[0]["reasons"] == ["probe_exception"]


async def test_completed_repository_observation_never_fetches(monkeypatch):
    module = importlib.import_module("src.doctor.integration_checks")
    read = AsyncMock(return_value={"entries": [], "unavailable": [], "projects": 0})
    monkeypatch.setattr(importlib.import_module("src.integration.delivery_branches"),
                        "observe_completed_branches", read)
    observer = object()
    monkeypatch.setattr(module, "_doctor_observer", lambda ctx: observer)
    ctx = DoctorContext(AppConfig(), db=object(), read_only=True)
    await module.completed_undelivered(ctx, project_ids={"p"}, now=NOW)
    read.assert_awaited_once_with(ctx.db, observer, project_ids={"p"}, now=NOW, cached_only=True)


def test_oversize_response_is_explicit_failure_without_truncated_success():
    response = bounded_inline({"observed_at": NOW, "scan_id": "scan", "payload": "x" * 262_144})
    assert response["error_code"] == "doctor.response_too_large"
    assert not response["success"] and not response["complete"]
