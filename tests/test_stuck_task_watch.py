"""Reviewed watch policy through the real engine, handler and disposable PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.contracts import CONTRACTS
from src.commands.contracts import builtin
from src.commands.handler import CommandHandler
from src.commands.principal import (
    TRUSTED_LOCAL, ExecutionPrincipal, PrincipalKind, principal_context,
)
from src.config import AppConfig, MessagesConfig, PlaybooksConfig
from src.database import Database, tables as t
from src.doctor.observations import partition
from src.models import Project, ProjectStatus, Task, TaskStatus
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.private_values import PrivateValueService
from src.playbooks.run_state import RunLifecycle
from src.playbooks.services import DatabaseActivationSource
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn
from tests.playbook_v2_engine_helpers import RecordingBus, artifact_ref_for
from tests.test_v2_engine_repository import seed_artifact

FIXTURES = Path("tests/fixtures/playbooks/v2")
COMMANDS = frozenset({"list_projects", "list_tasks", "get_stuck_tasks", "doctor_run",
                      "playbook_state_list", "playbook_state_notify", "message_send"})
KINDS = [("claims.holder_consistency", "holder_inconsistency")] + [
    ("stall.sweep", kind) for kind in (
        "unclaimed_work", "undelivered_blocker", "restore_branch", "worker_relaunch_loop")]


class Scenario:
    def __init__(self, db, handler, store, private):
        self.db, self.handler, self.store, self.private = db, handler, store, private
        self.now = 1_800_000_000.0
        self.stuck = []
        self.findings = []
        self.incomplete = set()
        self.calls = []
        self.doctor_projects = ["p"]
        self.bus = RecordingBus()
        self.principal = ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK, policy=CapabilityPolicy(aq_commands=COMMANDS))

    async def stuck_observation(self, args, **kwargs):
        self.calls.append(("stuck", dict(args)))
        return {"success": True, "stuck": [row for row in self.stuck
                if row["project_id"] == args["project_id"]], "now_used": args["now"],
                "thresholds": {"assigned": 1800, "in_progress": 7200}}

    async def doctor_observation(self, registry, ctx, **kwargs):
        self.calls.append(("doctor", {"now": ctx.reference_time, **kwargs}))
        coverage = [partition(check, kind, project, complete=kind not in self.incomplete)
                    for project in self.doctor_projects for check, kind in KINDS]
        return {"success": not self.incomplete, "complete": not self.incomplete,
                "checks": [], "findings": list(self.findings),
                "identity_manifest": [{k: v for k, v in row.items()
                                       if k not in {"data", "detail", "severity"}}
                                      for row in self.findings],
                "coverage": coverage, "observed_at": ctx.reference_time,
                "summary": {"ok": 0, "info": 0, "warn": len(self.findings), "error": 0,
                            "fixes_applied": 0}, "exit_code": 0, "scan_id": "scan",
                "reasons": ["injected"] if self.incomplete else []}

    async def load(self, name="stuck-task-watch"):
        folder = FIXTURES / name
        artifact = PlaybookDefinition.model_validate_json((folder / "artifact.json").read_text())
        await self.install(artifact, (folder / "source.md").read_text())

    async def install(self, artifact, source):
        self.ref = artifact_ref_for(artifact)
        self.artifact = artifact
        self.store.put(artifact, source_digest=self.ref.source_digest,
                       contract_fingerprint=self.ref.contract_fingerprint,
                       profile_fingerprint="", compiler_build=self.ref.compiler_build)
        self.store.put_source(self.ref.artifact_sha256, source)
        await seed_artifact(self.db, self.ref)
        self.restart()

    def restart(self):
        self.engine = PlaybookEngine(
            services=EngineServices(contracts=CONTRACTS, clock=lambda: self.now,
                                    artifact_store=self.store, private_values=self.private,
                                    handler=self.handler, db=self.db, bus=self.bus),
            runs=self.db, waits=None, activations=DatabaseActivationSource(self.db))
        self.handler._v2_engine = lambda: self.engine

    async def tick(self, name, *, mode=ExecutionMode.LIVE, success=True):
        event = {"event_type": "timer.15m", "event_id": name,
                 "tick_time": datetime.fromtimestamp(self.now, timezone.utc).isoformat()}
        result = await self.engine.run_rule(self.ref, "watch-stalled-work", event,
                                            self.principal, mode, dispatch_id=name)
        snapshot = await self.db.load_run(result.run_id) if mode == ExecutionMode.LIVE else None
        if success and snapshot:
            receipts = await self.db.list_receipts(result.run_id)
            assert snapshot.lifecycle == RunLifecycle.COMPLETED, [
                (r.step_id, r.outcome, r.result, r.error) for r in receipts[-5:]]
        return result

    async def rows(self, table):
        async with self.db._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(select(table))).mappings()]

    async def messages(self):
        return await self.rows(t.messages)


def stuck(task="t", status="ASSIGNED", age=1801, **kw):
    return {"id": task, "task_id": task, "project_id": "p", "status": status,
            "seconds_in_state": age, "reason": "OLD", **kw}


@pytest.fixture
async def scenario(tmp_path, monkeypatch):
    db = Database(lease_dsn("stuck-task-watch"))
    await db.initialize()
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    config = AppConfig(data_dir=str(tmp_path), messages=MessagesConfig(enabled=True),
                       playbooks=PlaybooksConfig(enabled=True))
    handler = CommandHandler(SimpleNamespace(db=db, config=config, plugin_registry=None,
                                             bus=SimpleNamespace(emit=AsyncMock())), config,
                             doctor_registry=SimpleNamespace())
    scenario = Scenario(db, handler, ArtifactStore(config.compiled_root), private)
    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    monkeypatch.setattr(handler, "_get_stuck_tasks_observation", scenario.stuck_observation)
    monkeypatch.setattr("src.observations.commands.collect_observations", scenario.doctor_observation)
    await db.create_project(Project(id="p", name="Project", supervisor_enabled=True))
    await scenario.load()
    yield scenario
    await db.close()


async def test_three_ticks_suppress_replay_clear_and_recur(scenario, monkeypatch):
    from src.database.queries import playbook_state_queries

    transaction_clock = AsyncMock(return_value=1000.0)
    monkeypatch.setattr(playbook_state_queries, "transaction_time", transaction_clock)
    scenario.stuck = [stuck()]
    await scenario.tick("one")
    assert len(await scenario.messages()) == 1
    message_id = (await scenario.messages())[0]["id"]
    await scenario.tick("one")
    assert [row["id"] for row in await scenario.messages()] == [message_id]
    # Null repeat remains silent even well past the reminder fixture's interval.
    transaction_clock.return_value = 1_000_000.0
    for tick in ("two", "three"):
        scenario.now += 900
        scenario.stuck[0]["seconds_in_state"] += 900
        await scenario.tick(tick)
        assert len(await scenario.messages()) == 1
    scenario.now += 900
    scenario.stuck = []
    await scenario.tick("clear")
    assert len(await scenario.messages()) == 2
    scenario.now += 900
    scenario.stuck = [stuck()]
    await scenario.tick("recur")
    assert len(await scenario.messages()) == 3
    rows = await scenario.rows(t.playbook_state_entries)
    assert rows[0]["episode"] == 2 and rows[0]["active"]


async def test_empty_inventory_and_empty_scans_complete_without_messages(scenario):
    await scenario.tick("empty")
    assert not await scenario.messages()
    assert not await scenario.rows(t.playbook_state_entries)
    assert [name for name, _ in scenario.calls] == ["doctor", "stuck"]


async def test_two_active_projects_and_one_inactive_keep_sources_separate(scenario):
    for project, status in (("q", ProjectStatus.ACTIVE), ("sleeping", ProjectStatus.PAUSED)):
        await scenario.db.create_project(Project(id=project, name=project, status=status))
    scenario.doctor_projects = ["p", "q", "sleeping"]
    for project in scenario.doctor_projects:
        scenario.stuck.append(stuck("shared", project_id=project))
        scenario.findings.extend(finding(kind=kind, project=project) for _, kind in KINDS)
        for index, age in enumerate((0, 100000)):
            await scenario.db.create_task(Task(
                id=f"{project}-blocked-{index}", project_id=project, title="Blocked",
                description="", status=TaskStatus.BLOCKED, updated_at=scenario.now-age))
    await scenario.tick("projects")
    rows = await scenario.rows(t.playbook_state_entries)
    expected = {f"task:{project}:stuck:shared" for project in ("p", "q")}
    expected |= {f"task:{project}:blocked:{project}-blocked-{i}"
                 for project in ("p", "q") for i in range(2)}
    expected |= {finding(kind=kind, project=project)["key"]
                 for project in ("p", "q") for _, kind in KINDS}
    assert {row["key"] for row in rows} == expected
    assert len(await scenario.messages()) == len(expected)
    scenario.now += 900
    scenario.findings.reverse()
    scenario.stuck.reverse()
    await scenario.tick("reordered")
    assert len(await scenario.messages()) == len(expected)
    assert all(row["episode"] == 1 and row["revision"] == 2
               for row in await scenario.rows(t.playbook_state_entries))


async def test_exact_age_predicate_payload_manifest_and_all_blocked(scenario):
    scenario.stuck = [stuck("assigned-boundary", age=1800),
                      stuck("assigned-old", age=1801),
                      stuck("progress-boundary", "IN_PROGRESS", 7200),
                      stuck("progress-old", "IN_PROGRESS", 7201),
                      stuck("young-orphan", age=1300, kind="ORPHANED"),
                      stuck("old-orphan", age=1900, kind="ORPHANED"),
                      stuck("ready", "READY", 10000, kind="STUCK READY")]
    await scenario.db.create_task(Task(id="blocked", project_id="p", title="Just blocked",
                                       description="", status=TaskStatus.BLOCKED))
    await scenario.tick("qualification")
    rows = await scenario.rows(t.playbook_state_entries)
    assert {row["key"] for row in rows} == {
        "task:p:stuck:assigned-old", "task:p:stuck:progress-old",
        "task:p:stuck:old-orphan", "task:p:blocked:blocked"}
    assert len(await scenario.messages()) == 4
    scenario.now += 900
    await scenario.tick("unchanged")
    assert len(await scenario.messages()) == 4
    assert all(row["active"] for row in await scenario.rows(t.playbook_state_entries))


def finding(index=0, *, kind="holder_inconsistency", project="p", detail="Changed evidence"):
    check = "claims.holder_consistency" if kind == "holder_inconsistency" else "stall.sweep"
    # Keys originate at the observation seam; the policy must preserve them verbatim.
    return {"key": f"doctor:v1:{project}:{kind}:{index}", "check_id": check,
            "kind": kind, "kind_version": 1, "project_id": project,
            "subject": {"type": "task", "id": f"task-{index}"}, "severity": "warn",
            "detail": detail, "data": {"age": 100}}


async def test_doctor_kinds_are_independent_and_keys_survive_payload_changes(scenario):
    scenario.findings = [finding(kind=kind) for _, kind in KINDS]
    await scenario.tick("all-kinds")
    assert len(await scenario.messages()) == 5
    scenario.now += 900
    scenario.findings[0].update(detail="Different details", severity="error")
    scenario.findings[0]["data"]["age"] = 200
    await scenario.tick("changed-payload")
    assert len(await scenario.messages()) == 5
    scenario.now += 900
    scenario.findings = []
    scenario.incomplete = {"holder_inconsistency", "restore_branch"}
    await scenario.tick("partial")
    assert len(await scenario.messages()) == 8
    active = {row["key"] for row in await scenario.rows(t.playbook_state_entries) if row["active"]}
    assert active == {finding(kind=kind)["key"] for kind in scenario.incomplete}
    scenario.now += 900
    scenario.incomplete.clear()
    await scenario.tick("complete")
    assert len(await scenario.messages()) == 10
    assert not any(row["active"] for row in await scenario.rows(t.playbook_state_entries))


async def test_inactive_project_retains_episode_without_new_notifications(scenario):
    scenario.stuck = [stuck()]
    scenario.findings = [finding()]
    await scenario.tick("active")
    before = await scenario.rows(t.playbook_state_entries)
    async with scenario.db.immediate() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(status="PAUSED"))
    scenario.now += 900
    scenario.stuck = []
    scenario.findings = []
    await scenario.tick("paused")
    assert await scenario.rows(t.playbook_state_entries) == before
    assert len(await scenario.messages()) == 2
    async with scenario.db.immediate() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(status="ACTIVE"))
    scenario.now += 900
    await scenario.tick("reactivated")
    assert len(await scenario.messages()) == 4


@pytest.mark.parametrize("command", ["list_projects", "playbook_state_list", "get_stuck_tasks",
                                      "list_tasks", "doctor_run"])
async def test_incomplete_scan_cannot_clear_its_partition(scenario, monkeypatch, command):
    scenario.stuck = [stuck()]
    scenario.findings = [finding()]
    await scenario.db.create_task(Task(id="b", project_id="p", title="Blocked",
                                       description="", status=TaskStatus.BLOCKED))
    await scenario.tick("seed")
    scenario.now += 900
    scenario.stuck = []
    scenario.findings = []
    async with scenario.db.immediate() as conn:
        await conn.execute(update(t.tasks).values(status="COMPLETED"))
    original = getattr(scenario.handler, "_cmd_" + command)

    async def incomplete(args):
        result = await original(args)
        result.update(success=False, complete=False, error_code="injected-incomplete")
        if command == "doctor_run":
            result["coverage"] = [dict(part, complete=False) for part in result["coverage"]]
        return result

    monkeypatch.setattr(scenario.handler, "_cmd_" + command, incomplete)
    await scenario.tick("incomplete", success=command not in {"list_projects", "playbook_state_list"})
    rows = await scenario.rows(t.playbook_state_entries)
    if command in {"list_projects", "playbook_state_list"}:
        assert all(row["active"] and row["revision"] == 1 for row in rows)
        assert len(await scenario.messages()) == 3
    else:
        source = {"get_stuck_tasks": "stuck", "list_tasks": "blocked",
                  "doctor_run": "doctor:claims.holder_consistency:holder_inconsistency"}[command]
        assert [row["source_partition"]["source"] for row in rows if row["active"]] == [source]
        assert len(await scenario.messages()) == 5


@pytest.mark.parametrize("failure", ["timeout", "exception", "dependency", "truncated"])
async def test_failed_doctor_producer_never_authorizes_absence(scenario, monkeypatch, failure):
    scenario.findings = [finding()]
    await scenario.tick("seed")
    scenario.now += 900
    scenario.findings = []

    async def unavailable(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("diagnostic timeout")
        if failure == "exception":
            raise RuntimeError("diagnostic exception")
        result = await scenario.doctor_observation(*args, **kwargs)
        result.update(success=False, complete=False,
                      reasons=["dependency_unavailable" if failure == "dependency" else "truncated"])
        result["coverage"] = [dict(row, complete=False) for row in result["coverage"]]
        return result

    monkeypatch.setattr("src.observations.commands.collect_observations", unavailable)
    await scenario.tick("failure", success=False)
    assert len(await scenario.messages()) == 1
    row, = await scenario.rows(t.playbook_state_entries)
    assert row["active"] and row["revision"] == 1


@pytest.mark.parametrize("error", ["snapshot.page_unavailable", "snapshot.expired"])
async def test_page_failure_cannot_clear_from_partial_traversal(scenario, monkeypatch, error):
    from src.observations.snapshots import SnapshotError, SnapshotService

    scenario.stuck = [stuck("gone"), stuck("keep")]
    await scenario.tick("seed")
    scenario.now += 900
    scenario.stuck = [stuck("keep")]
    original = SnapshotService.read

    async def unreadable(self, **args):
        if args["producer"] == "get_stuck_tasks":
            raise SnapshotError(error)
        return await original(self, **args)

    monkeypatch.setattr(SnapshotService, "read", unreadable)
    await scenario.tick("page-failure", success=False)
    assert len(await scenario.messages()) == 2
    assert all(row["active"] for row in await scenario.rows(t.playbook_state_entries))


async def test_more_than_fifty_findings_and_multiple_payload_state_pages(scenario):
    # Payload size forces actual immutable paging without changing the recorded page size.
    scenario.stuck = [stuck(f"task-{i}", detail="x" * 6000) for i in range(60)]
    scenario.findings = [finding(i, detail="x" * 6000) for i in range(60)]
    await scenario.tick("many")
    assert len(await scenario.messages()) == 120
    snapshots = await scenario.rows(t.observation_snapshots)
    pages = await scenario.rows(t.observation_snapshot_pages)
    for producer in ("get_stuck_tasks", "doctor_run"):
        ids = {row["snapshot_id"] for row in snapshots if row["producer"] == producer}
        assert sum(row["snapshot_id"] in ids for row in pages) > 1
    scenario.now += 900
    scenario.stuck = []
    scenario.findings = []
    await scenario.tick("clear-many")
    assert len(await scenario.messages()) == 240
    assert not any(row["active"] for row in await scenario.rows(t.playbook_state_entries))
    snapshots = await scenario.rows(t.observation_snapshots)
    pages = await scenario.rows(t.observation_snapshot_pages)
    state_ids = {row["snapshot_id"] for row in snapshots
                 if row["producer"] == "playbook_state_list"}
    assert any(sum(page["snapshot_id"] == snapshot for page in pages) > 1
               for snapshot in state_ids)


async def test_restart_preserves_suppression_and_failed_wake_is_durable(scenario):
    from src.messages.delivery import MessageDeliveryEngine

    scenario.handler.orchestrator.bus.emit.side_effect = RuntimeError("wake lost")
    scenario.stuck = [stuck()]
    await scenario.tick("before-restart")
    messages = await scenario.messages()
    assert len(messages) == 1 and messages[0]["to_id"] == "supervisor-global"
    scenario.restart()
    scenario.now += 900
    await scenario.tick("after-restart")
    assert len(await scenario.messages()) == 1
    assert (await scenario.rows(t.playbook_state_entries))[0]["revision"] == 2
    sessions = SimpleNamespace(activity=AsyncMock(return_value="idle"),
                               nudge=AsyncMock(return_value=True))
    delivery = MessageDeliveryEngine(scenario.db, sessions, scenario.handler.config)
    await delivery.run_delivery_pass()
    sessions.nudge.assert_awaited_once()
    assert messages[0]["id"] in sessions.nudge.call_args.kwargs["text"]
    assert (await scenario.messages())[0]["notification_attempts"] == 1


async def test_restart_replays_committed_notification_without_completion_receipt(scenario, monkeypatch):
    from src.playbooks.engine import InterruptedByRestart, OperatorResolution

    class SimulatedCrash(BaseException):
        pass

    scenario.stuck = [stuck()]
    original = scenario.db.commit_boundary
    with monkeypatch.context() as patch:
        async def lose_receipt(snapshot, receipt, *args, **kwargs):
            if receipt.step_id == "stuck_present" and receipt.completed_at is not None:
                raise SimulatedCrash()
            return await original(snapshot, receipt, *args, **kwargs)

        patch.setattr(scenario.db, "commit_boundary", lose_receipt)
        with pytest.raises(SimulatedCrash):
            await scenario.tick("crash")
    messages = await scenario.messages()
    assert len(messages) == 1
    before = await scenario.rows(t.playbook_state_operations)
    run = await scenario.db.find_run_for_dispatch("stuck-task-watch", "crash", "watch-stalled-work")
    assert run.current_step_id == "stuck_present"
    scenario.restart()
    scenario.now += 1
    result = await scenario.engine.resume(
        run.run_id, InterruptedByRestart(scenario.now), scenario.principal)
    # The existing generic contract requires an operator to resolve ambiguous effects.
    # Retrying that exact observation must still replay the committed journal entry.
    assert result.lifecycle == RunLifecycle.PAUSED
    result = await scenario.engine.resume(
        run.run_id, OperatorResolution(kind="retry"), TRUSTED_LOCAL)
    assert result.lifecycle == RunLifecycle.COMPLETED
    assert [row["id"] for row in await scenario.messages()] == [messages[0]["id"]]
    assert await scenario.rows(t.playbook_state_operations) == before
    assert (await scenario.rows(t.playbook_state_entries))[0]["revision"] == 1


@pytest.mark.parametrize("grant", ["playbook_state_notify", "message_send"])
async def test_original_principal_must_authorize_each_effect(scenario, grant):
    from dataclasses import replace

    scenario.principal = replace(scenario.principal,
                                 policy=CapabilityPolicy(aq_commands=COMMANDS - {grant}))
    scenario.stuck = [stuck()]
    await scenario.tick("denied", success=False)
    assert not await scenario.messages()
    assert not await scenario.rows(t.playbook_state_entries)
    assert not await scenario.rows(t.playbook_state_operations)


async def test_stale_absence_uses_saved_revision_and_defers_without_reread(scenario, monkeypatch):
    scenario.stuck = [stuck()]
    await scenario.tick("seed")
    scenario.now += 900
    scenario.stuck = []
    original = scenario.handler._cmd_get_stuck_tasks
    injected = False

    async def newer_presence(args):
        nonlocal injected
        result = await original(args)
        if not args.get("snapshot_id") and not injected:
            injected = True
            scenario.now += 900
            scenario.stuck = [stuck()]
            await scenario.tick("newer-present")
            scenario.stuck = []
        return result

    monkeypatch.setattr(scenario.handler, "_cmd_get_stuck_tasks", newer_presence)
    await scenario.tick("older-absence")
    rows = await scenario.rows(t.playbook_state_entries)
    assert len(rows) == 1 and rows[0]["active"] and rows[0]["revision"] == 2
    assert rows[0]["last_observation_id"] == "newer-present"
    assert len(await scenario.messages()) == 1
    scenario.now += 900
    await scenario.tick("next-complete-scan")
    assert len(await scenario.messages()) == 2
    assert not (await scenario.rows(t.playbook_state_entries))[0]["active"]


async def test_reminder_exact_boundary_concurrency_and_old_tick_replay(scenario, monkeypatch):
    import asyncio

    from src.database.queries import playbook_state_queries

    await scenario.load("stuck-task-watch-reminder")
    transaction_clock = AsyncMock(return_value=1000.0)
    monkeypatch.setattr(playbook_state_queries, "transaction_time", transaction_clock)
    scenario.stuck = [stuck()]
    await scenario.tick("initial")
    transaction_clock.return_value = 87399.0
    scenario.now += 900
    await scenario.tick("before-boundary")
    assert len(await scenario.messages()) == 1
    transaction_clock.return_value = 87400.0
    scenario.now += 900
    # Independent dispatches at the same frozen time can commit only one reminder.
    await asyncio.gather(*(scenario.tick(f"due-{i}", success=False) for i in range(3)))
    assert len(await scenario.messages()) == 2
    transaction_clock.return_value = 999999.0
    await scenario.tick("initial")
    assert len(await scenario.messages()) == 2


@pytest.mark.parametrize("fault", ["message", "private", "keys", "journal", "namespace"])
async def test_notification_failure_rolls_back_all_effects_and_next_tick_retries(
        scenario, monkeypatch, fault):
    from src.playbooks.private_values import PrivateValueBatch
    from src.playbooks.state import Limits
    from src.playbooks.state_notify import NotificationService

    scenario.stuck = [stuck()]
    with monkeypatch.context() as patch:
        if fault == "message":
            original = scenario.db.create_message_in_transaction

            async def fail(conn, **kwargs):
                await original(conn, **kwargs)
                raise RuntimeError("crash after message insert")

            patch.setattr(scenario.db, "create_message_in_transaction", fail)
        elif fault == "private":
            original = PrivateValueBatch.persist

            async def fail(self, conn):
                await original(self, conn)
                if self.owner.kind == "namespace":
                    raise RuntimeError("crash before protected state commit")

            patch.setattr(PrivateValueBatch, "persist", fail)
        else:
            limits = {"keys": Limits(keys=0), "journal": Limits(journal_bytes=1),
                      "namespace": Limits(namespace_bytes=1)}[fault]
            original = NotificationService.__init__

            def init(self, engine, private):
                original(self, engine, private, limits=limits)

            patch.setattr(NotificationService, "__init__", init)
        await scenario.tick("fault", success=False)
    assert not await scenario.messages()
    assert not await scenario.rows(t.playbook_state_entries)
    assert not await scenario.rows(t.playbook_state_operations)
    scenario.now += 900
    await scenario.tick("retry")
    assert len(await scenario.messages()) == 1


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.SHADOW])
async def test_preview_uses_no_durable_state_or_message_effects(scenario, mode):
    before = {table.name: await scenario.rows(table) for table in (
        t.messages, t.playbook_state_entries, t.playbook_state_operations,
        t.observation_snapshots, t.playbook_private_values, t.jobs)}
    await scenario.tick("preview", mode=mode)
    assert before == {table.name: await scenario.rows(table) for table in (
        t.messages, t.playbook_state_entries, t.playbook_state_operations,
        t.observation_snapshots, t.playbook_private_values, t.jobs)}


@pytest.mark.parametrize("name,interval", [("stuck-task-watch", None),
                                           ("stuck-task-watch-reminder", 86400)])
def test_reviewed_recordings_are_pinned_opt_in_and_only_use_generic_steps(name, interval):
    from src.playbooks.definition import artifact_sha256, source_digest
    from src.playbooks.required import INITIAL_SYSTEM_PLAYBOOK_IDS, REQUIRED_SYSTEM_PLAYBOOK_IDS

    folder = FIXTURES / name
    artifact = PlaybookDefinition.model_validate_json((folder / "artifact.json").read_text())
    assert artifact_sha256(artifact) == (folder / "artifact.sha256").read_text().strip()
    assert artifact.source_hash == source_digest((folder / "source.md").read_text())
    assert artifact.scope.type == "system"
    assert artifact.trusted_execution.state_schema_version == 1
    assert artifact.rules[0].trigger.event_type == "timer.15m"
    assert name not in INITIAL_SYSTEM_PLAYBOOK_IDS and name not in REQUIRED_SYSTEM_PLAYBOOK_IDS
    assert {step.type for step in artifact.steps.values()} == {
        "command", "decision", "foreach", "terminal"}
    commands = {step.command for step in artifact.steps.values() if step.type == "command"}
    assert commands == COMMANDS - {"message_send"}
    for step in artifact.steps.values():
        if step.type == "command" and step.command == "playbook_state_notify":
            repeat = step.inputs.get("repeat_after_seconds")
            assert (repeat.value if repeat else None) == interval
    for path in folder.iterdir():
        assert path.read_bytes() == (Path("src/prompts/reviewed_playbooks") / name / path.name).read_bytes()


async def test_all_scans_share_frozen_tick_despite_wall_clock_changes(scenario, monkeypatch):
    scenario.stuck = [stuck()]
    original = scenario.stuck_observation

    async def delayed(args, **kwargs):
        scenario.now += 500
        return await original(args, **kwargs)

    monkeypatch.setattr(scenario.handler, "_get_stuck_tasks_observation", delayed)
    frozen = scenario.now
    await scenario.tick("frozen")
    snapshots = await scenario.rows(t.observation_snapshots)
    # State snapshot time is the transaction timestamp used for TTL, not an observation.
    observations = [row for row in snapshots if row["producer"] != "playbook_state_list"]
    assert {row["observed_at"] for row in observations} == {frozen}
    assert {row["last_observed_at"] for row in await scenario.rows(t.playbook_state_entries)} == {frozen}
    assert {row["last_observation_id"] for row in await scenario.rows(t.playbook_state_entries)} == {"frozen"}
    assert all(args["now"] == frozen for _, args in scenario.calls)
    doctor_args = next(args for name, args in scenario.calls if name == "doctor")
    assert doctor_args["group"] == "task_observations_v1"


async def test_disabled_reenabled_and_recompiled_policy_retains_namespace(scenario):
    from src.playbooks.authoring import PlaybookSource
    from src.playbooks.profiles import shipped_profile_lookup
    from src.playbooks.proposal import propose
    from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup

    async def activate():
        with principal_context(TRUSTED_LOCAL):
            result = await scenario.handler.execute("playbook_activate", {
                "playbook_id": scenario.artifact.id,
                "artifact_sha256": scenario.ref.artifact_sha256,
                "acknowledge_diff": scenario.ref.artifact_sha256})
        assert result["success"], result

    await activate()
    scenario.stuck = [stuck()]
    await scenario.tick("original")
    with principal_context(TRUSTED_LOCAL):
        result = await scenario.handler.execute("set_playbook_enabled", {
            "playbook_id": scenario.artifact.id, "enabled": False})
    assert result["success"], result
    scenario.restart()
    dispatched = await scenario.engine.dispatch_event(
        {"event_type": "timer.15m", "event_id": "disabled"}, scenario.principal)
    assert not dispatched.run_ids
    assert len(await scenario.messages()) == 1
    with principal_context(TRUSTED_LOCAL):
        result = await scenario.handler.execute("set_playbook_enabled", {
            "playbook_id": scenario.artifact.id, "enabled": True})
    assert result["success"], result
    scenario.now += 900
    await scenario.tick("enabled-again")
    assert len(await scenario.messages()) == 1
    source_path = Path("src/prompts/reviewed_playbooks/stuck-task-watch/source.md")
    source = PlaybookSource.load(source_path, vault_root=Path.cwd())
    raw = scenario.artifact.model_dump(mode="json")
    proposal = propose(source, {"rules": raw["rules"], "steps": raw["steps"]},
                       contracts=RegistryContractLookup(), profiles=shipped_profile_lookup(),
                       events=RegisteredEventLookup(), version=2, enforce_inventory=True)
    assert proposal.artifact is not None, proposal.diagnostics
    old_sha = scenario.ref.artifact_sha256
    await scenario.install(proposal.artifact, source_path.read_text())
    assert scenario.ref.artifact_sha256 != old_sha
    await activate()
    scenario.restart()
    scenario.now += 900
    await scenario.tick("recompiled")
    assert len(await scenario.messages()) == 1
    rows = await scenario.rows(t.playbook_state_entries)
    assert len(rows) == 1 and rows[0]["episode"] == 1 and rows[0]["revision"] == 3


@pytest.mark.integration
def test_reviewed_watch_through_disposable_daemon(tmp_path):
    """Import, activate and execute the exact recording over the real daemon API."""
    import asyncio
    import json
    import shutil
    import time
    from unittest.mock import patch

    import yaml
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.test_e2e_cli_stateful import disposable_world, world_env

    env = world_env(tmp_path / "aq-e2e")
    fragment = tmp_path / "watch-config.yaml"
    fragment.write_text("swarm:\n  enabled: false\n")
    env["AQ_E2E_EXTRA_CONFIG"] = str(fragment)
    with disposable_world("watch", env) as world, patch.dict("os.environ", env):
        home = Path(env["AQ_E2E_HOME"])
        config = yaml.safe_load((home / "config.yaml").read_text())
        bundle = home / "vault" / "acceptance" / "stuck-task-watch"
        shutil.copytree(FIXTURES / "stuck-task-watch", bundle)
        api = world.smoke.api_checked
        imported = api("playbook_v2_import", {"path": str(bundle)})
        sha = (bundle / "artifact.sha256").read_text().strip()
        assert sha in json.dumps(imported)
        api("playbook_activate", {"playbook_id": "stuck-task-watch",
                                  "artifact_sha256": sha, "acknowledge_diff": sha})
        task = world.smoke.create_task("F2 blocked observation")
        api("set_task_status", {"task_id": task, "status": "BLOCKED"})

        async def evidence():
            engine = create_async_engine(config["database"]["url"])
            try:
                async with engine.connect() as conn:
                    records = {}
                    for table in (t.messages, t.playbook_state_entries,
                                  t.playbook_state_operations, t.playbook_step_receipts,
                                  t.observation_snapshots):
                        records[table.name] = [dict(row) for row in
                            (await conn.execute(select(table))).mappings()]
                    return records
            finally:
                await engine.dispose()

        def tick(identity):
            # Manual runs expose handled failures even when the policy completes.
            # A bounded publisher log cannot prove absence, so the real doctor
            # snapshot and its pages are partial; the watch checks each partition.
            result = world.smoke.api("run_playbook", {"playbook_id": "stuck-task-watch", "event": {
                "type": "timer.15m", "event_id": identity,
                "tick_time": datetime.now(timezone.utc).isoformat()}})
            assert result["status"] == "completed", result
            assert result["success"] is False
            failures = result["failed_steps"]
            assert any(row["step_id"] == "doctor_scan" for row in failures)
            assert all(row["step_id"] in {"doctor_scan", "doctor_page"}
                       and row["outcome"] == "failure" for row in failures), result
            snapshots = asyncio.run(evidence())[t.observation_snapshots.name]
            doctor, = [row for row in snapshots if row["run_id"] == result["run_id"]
                       and row["producer"] == "doctor_run"]
            assert doctor["complete"] is False
            assert "bounded_log_tail" in doctor["envelope"]["reasons"]
            assert all(part["kind"] in {"publisher_skip", "publisher_error"}
                       for part in doctor["coverage"]
                       if "bounded_log_tail" in part["reasons"])
            selected = [part for part in doctor["coverage"] if part["project_id"] == "e2e"
                        and (part["check_id"], part["kind"]) in KINDS]
            assert {(part["check_id"], part["kind"]) for part in selected} == set(KINDS)
            assert all(part["complete"] for part in selected), selected
            return result

        key = f"task:e2e:blocked:{task}"
        initial = tick("daemon-first")
        assert initial["status"] == "completed"
        first = asyncio.run(evidence())
        row = next(row for row in first[t.playbook_state_entries.name] if row["key"] == key)
        message_id = row["message_id"]
        assert row["active"] and row["revision"] == 1 and row["episode"] == 1
        assert any(row["id"] == message_id for row in first[t.messages.name])
        assert first[t.playbook_state_operations.name] and first[t.playbook_step_receipts.name]
        # Distinct observations must have increasing watermarks.
        for identity in ("daemon-second", "daemon-third"):
            time.sleep(0.01)
            assert tick(identity)["status"] == "completed"
        replay = tick("daemon-first")
        assert replay["run_id"] == initial["run_id"]
        unchanged = asyncio.run(evidence())
        row = next(row for row in unchanged[t.playbook_state_entries.name] if row["key"] == key)
        assert row["revision"] == 3 and row["episode"] == 1 and row["message_id"] == message_id
        assert sum(row["id"] == message_id for row in unchanged[t.messages.name]) == 1
        api("set_task_status", {"task_id": task, "status": "DEFINED"})
        assert tick("daemon-clear")["status"] == "completed"
        cleared = asyncio.run(evidence())
        row = next(row for row in cleared[t.playbook_state_entries.name] if row["key"] == key)
        assert not row["active"] and row["revision"] == 4 and row["message_id"] != message_id
