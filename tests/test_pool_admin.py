"""Reusable pool edits preserve single-profile command semantics and audit data."""

from __future__ import annotations

from pathlib import Path
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.commands.pool_admin import set_pool_bounds, set_pool_enabled, set_pool_lifecycle
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.event_schemas import get_schema, validate_payload
from src.models import Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.profiles.parser import parse_profile
from src.profiles.sync import sync_profile_text_to_db
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def handler(tmp_path):
    db = Database(lease_dsn("pool-admin.db"))
    await db.initialize()
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database=DatabaseConfig(url=lease_dsn("pool-admin.db")),
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    cfg.swarm.enabled = True
    cfg.swarm.global_max_active = 5
    orch = Orchestrator(cfg)
    orch.db = db
    orch.bus = EventBus(env="dev")
    for project_id, cap in (("a", 2), ("b", 4)):
        await db.create_project(Project(id=project_id, name=project_id, max_concurrent_agents=cap))
    path = tmp_path / "data/vault/agent-types/worker/profile.md"
    path.parent.mkdir(parents=True)
    markdown = """---
id: worker
name: Worker
---
## Role
Work carefully.
## Config
```json
{"harness": "fake", "lifecycle": "pool", "min_active": 1, "max_active": 3,
 "min_per_project": 1, "max_claims_per_session": 2}
```
"""
    path.write_text(markdown, encoding="utf-8")
    synced = await sync_profile_text_to_db(markdown, db, source_path=str(path))
    assert synced.success, synced.errors
    try:
        yield CommandHandler(orch, cfg)
    finally:
        await db.close()


def collect_events(handler):
    events = []
    for event_type in (
        "pool.lifecycle_changed",
        "pool.bounds_changed",
        "pool.enabled_changed",
        "pool.session_drained",
    ):
        handler.orchestrator.bus.subscribe(
            event_type,
            lambda payload: events.append(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"event_id", "_event_type"}
                }
            ),
        )
    return events


async def session(handler, session_id, *, project_id="a", task_id=None, started_at=1):
    row = SessionRecord(
        id=session_id,
        project_id=project_id,
        profile_id="worker",
        harness="fake",
        provider="fake",
        name=session_id,
        lifecycle="pool",
        state="running",
        work_dir=f"{handler.config.workspace_dir}/{session_id}",
        epoch="test",
        instance_token=session_id,
        task_id=task_id,
        started_at=started_at,
    )
    await handler.db.create_session(row)
    return row


async def test_lifecycle_returns_rows_and_gracefully_drains_every_project(handler):
    await handler.db.create_task(
        Task(
            id="busy-task",
            project_id="b",
            title="Busy",
            description="Work",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await session(handler, "idle")
    await session(handler, "busy", project_id="b", task_id="busy-task")
    events = collect_events(handler)
    result = await set_pool_lifecycle(
        handler,
        {"profile_id": "worker", "lifecycle": "task"},
        correlation={"request_id": "allocation-1"},
    )
    assert result["success"] is True
    assert result["before"]["lifecycle"] == "pool"
    assert result["before"]["min_active"] == 1
    assert result["after"] == {
        **result["before"],
        "lifecycle": "task",
        "min_active": None,
        "max_active": None,
        "min_per_project": None,
        "max_claims_per_session": None,
    }
    assert {row["session_id"] for row in result["session_actions"]} == {"idle", "busy"}
    assert all(row["action"] == "drain" for row in result["session_actions"])
    for session_id in ("idle", "busy"):
        row = await handler.db.get_session(session_id)
        assert (row.state, row.desired_state) == ("running", "stopped")
    assert (await handler.db.get_task("busy-task")).status == TaskStatus.IN_PROGRESS
    assert len(events) == 4
    assert all(event["request_id"] == "allocation-1" for event in events)
    assert {event["project_id"] for event in events} == {"a", "b"}


async def test_enabled_returns_rows_and_preserves_the_pool(handler):
    events = collect_events(handler)
    result = await set_pool_enabled(
        handler,
        {"profile_id": "worker", "enabled": False},
        correlation={"request_id": "allocation-2"},
    )
    assert result["before"]["enabled"] is True
    assert result["after"] == {**result["before"], "enabled": False}
    assert result["session_actions"] == []
    assert (await handler.db.get_profile("worker")).enabled is False
    assert len(events) == 2
    assert all(event["request_id"] == "allocation-2" for event in events)


async def test_bounds_return_rows_and_effective_caps_without_draining(handler):
    events = collect_events(handler)
    result = await set_pool_bounds(
        handler,
        {"profile_id": "worker", "max": 8},
        correlation={"request_id": "allocation-3"},
    )
    assert result["after"] == {**result["before"], "max_active": 8}
    assert result["before"]["max_active"] == 3
    assert result["session_actions"] == result["terminated"] == []
    assert result["project_caps"] == [
        {"project_id": "a", "max_concurrent_agents": 2, "effective_max_active": 2},
        {"project_id": "b", "max_concurrent_agents": 4, "effective_max_active": 4},
    ]
    assert len(events) == 2
    assert all(event["request_id"] == "allocation-3" for event in events)


async def test_bounds_explicit_null_is_durable_and_keeps_the_minimum(handler, tmp_path):
    result = await set_pool_bounds(handler, {"profile_id": "worker", "max": None})
    assert result["after"] == {**result["before"], "max_active": None}
    assert (await handler.db.get_profile("worker")).max_active is None
    config = parse_profile(
        (tmp_path / "data/vault/agent-types/worker/profile.md").read_text(encoding="utf-8")
    ).config
    assert "max_active" not in config
    assert config["min_active"] == 1


@pytest.mark.parametrize("depth", ["profile", "directory"])
async def test_delete_pool_removes_configuration_and_respects_vault_depth(handler, depth):
    directory = Path(handler.config.data_dir) / "vault/agent-types/worker"
    memory = directory / "memory/note.md"
    memory.parent.mkdir()
    memory.write_text("Keep useful findings")
    events = []
    handler.orchestrator.bus.subscribe("pool.deleted", events.append)
    result = await handler.execute("pool_delete", {"profile_id": "worker", "depth": depth})
    assert result["success"] is True
    assert result["retired"] is True
    assert result["warnings"] == []
    assert await handler.db.get_profile("worker") is None
    assert not (directory / "profile.md").exists()
    assert memory.exists() == (depth == "profile")
    assert directory.exists() == (depth == "profile")
    assert (await handler._cmd_pool_status({}))["pools"] == []
    assert events[0]["profile_id"] == "worker"
    assert validate_payload("pool.deleted", events[0]) == []
    # An ambiguous network response is safe to retry, including deeper cleanup.
    repeated = await handler.execute("pool_delete", {"profile_id": "worker", "depth": "directory"})
    assert repeated["success"] is True
    assert not directory.exists()


async def test_delete_pool_stops_idle_and_busy_instances_across_projects(handler):
    await handler.db.create_task(Task(
        id="held", project_id="b", title="Held", description="d",
        status=TaskStatus.IN_PROGRESS, profile_id="worker", route_source="router",
    ))
    await session(handler, "idle")
    await session(handler, "busy", project_id="b", task_id="held")
    provider = MagicMock()
    provider.stop = AsyncMock()
    handler.orchestrator.session_providers.create = MagicMock(return_value=provider)
    result = await handler.execute("pool_delete", {"profile_id": "worker"})
    assert result["success"] is True
    assert set(result["sessions"]) == {"idle", "busy"}
    assert result["warnings"] == []
    assert provider.stop.await_count == 2
    for sid in ("idle", "busy"):
        row = await handler.db.get_session(sid)
        assert (row.state, row.desired_state) == ("stopped", "stopped")
    task = await handler.db.get_task("held")
    assert (task.status, task.profile_id, task.route_source) == (TaskStatus.READY, None, "unrouted")


async def test_delete_pool_returns_provider_holds_to_router_preserving_manual_pauses(handler):
    for task_id, status, resume_after in (
        ("held", TaskStatus.READY, None),
        ("backoff", TaskStatus.PAUSED, 9999999999),
        ("manual", TaskStatus.PAUSED, None),
    ):
        await handler.db.create_task(Task(
            id=task_id, project_id="a", title=task_id, description="d", status=status,
            profile_id="worker", route_source="router", provider_intent="pinned",
            resume_after=resume_after, intelligence_class="standard-high",
        ))
        await handler.db.set_task_meta(task_id, "provider_pause", {"provider": "codex"})
    result = await handler.execute("pool_delete", {"profile_id": "worker"})
    assert result["success"] is True
    for task_id in ("held", "backoff", "manual"):
        row = await handler.db.get_task(task_id)
        assert (row.profile_id, row.route_source, row.provider_intent) == (None, "unrouted", "class_only")
        assert row.intelligence_class == "standard-high"
        assert row.route["legacy"]["profile_id"] == "worker"
        assert row.status == (TaskStatus.PAUSED if task_id == "manual" else TaskStatus.READY)
    assert await handler.db.get_task_meta("backoff", "provider_pause") is None
    assert await handler.db.get_task_meta("manual", "provider_pause") is not None


async def test_delete_pool_warns_on_unconfirmed_stop_and_preserves_held_resources(handler):
    await handler.db.create_task(Task(
        id="held", project_id="a", title="Held", description="d",
        status=TaskStatus.IN_PROGRESS, profile_id="worker", route_source="router",
    ))
    await session(handler, "busy", task_id="held")
    provider = MagicMock()
    provider.stop = AsyncMock(side_effect=RuntimeError("cannot confirm stop"))
    handler.orchestrator.session_providers.create = MagicMock(return_value=provider)
    result = await handler.execute("pool_delete", {"profile_id": "worker"})
    assert result["success"] is True
    assert "still stopping" in result["warnings"][0]
    assert await handler.db.get_profile("worker") is None
    row = await handler.db.get_session("busy")
    assert (row.task_id, row.state, row.desired_state) == ("held", "running", "stopped")
    assert (await handler.db.get_task("held")).status == TaskStatus.IN_PROGRESS


async def test_delete_pool_tombstone_prevents_retained_source_reimport(handler, monkeypatch):
    source = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    original = source.read_text()
    unlink = Path.unlink

    def readonly(path, *args, **kwargs):
        if path == source:
            raise PermissionError("read-only source")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", readonly)
    result = await handler.execute("pool_delete", {"profile_id": "worker"})
    assert result["success"] is True
    assert "Vault cleanup incomplete" in result["warnings"][0]
    assert source.exists()
    synced = await sync_profile_text_to_db(original, handler.db, source_path=str(source))
    assert synced.success and synced.action == "none"
    assert await handler.db.get_profile("worker") is None


async def test_retirement_during_an_inflight_vault_import_cannot_resurrect_pool(handler, monkeypatch):
    source = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    original = source.read_text()
    upsert = handler.db.upsert_profile

    async def delayed_upsert(profile):
        result = await handler.execute("pool_delete", {"profile_id": "worker"})
        assert result["success"], result
        return await upsert(profile)

    monkeypatch.setattr(handler.db, "upsert_profile", delayed_upsert)
    synced = await sync_profile_text_to_db(original, handler.db, source_path=str(source))
    assert synced.success and synced.action == "none"
    assert await handler.db.get_profile("worker") is None


@pytest.mark.parametrize("profile_id", ["../worker", "x/y", "x\\y", ".", "..", ""])
async def test_delete_pool_validates_global_id_before_side_effects(handler, profile_id):
    result = await handler.execute("pool_delete", {"profile_id": profile_id})
    assert result["success"] is False
    assert await handler.db.get_profile("worker") is not None


async def test_bounds_now_terminates_oldest_idle_sessions_and_correlates_drains(handler):
    await handler.db.create_task(
        Task(
            id="busy-task",
            project_id="a",
            title="Busy",
            description="Work",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await session(handler, "busy", task_id="busy-task", started_at=0)
    await session(handler, "older", started_at=1)
    await session(handler, "newer", started_at=2)
    provider = MagicMock()
    provider.stop = AsyncMock()
    handler.orchestrator.session_providers.create = MagicMock(return_value=provider)
    events = collect_events(handler)
    result = await set_pool_bounds(
        handler,
        {"profile_id": "worker", "max": 2, "now": True},
        correlation={"request_id": "allocation-4"},
    )
    assert result["terminated"] == ["older"]
    assert result["session_actions"] == [
        {
            "project_id": "a",
            "profile_id": "worker",
            "session_id": "older",
            "action": "terminate",
            "reason": "scaled",
        }
    ]
    assert (await handler.db.get_session("older")).state == "stopped"
    assert (await handler.db.get_session("newer")).state == "running"
    assert (await handler.db.get_session("busy")).state == "running"
    assert (await handler.db.get_task("busy-task")).status == TaskStatus.IN_PROGRESS
    drained = [event for event in events if event.get("session_id") == "older"]
    assert len(drained) == 1
    assert all(event["request_id"] == "allocation-4" for event in events)


@pytest.mark.parametrize(
    "helper,command,args,error",
    [
        (set_pool_lifecycle, "_cmd_pool_set_lifecycle", {}, "profile_id is required"),
        (
            set_pool_lifecycle,
            "_cmd_pool_set_lifecycle",
            {"profile_id": "worker", "lifecycle": "named"},
            "lifecycle must be task or pool",
        ),
        (
            set_pool_lifecycle,
            "_cmd_pool_set_lifecycle",
            {"profile_id": "missing", "lifecycle": "task"},
            "no profile 'missing'",
        ),
        (set_pool_enabled, "_cmd_pool_set_enabled", {}, "profile_id is required"),
        (
            set_pool_enabled,
            "_cmd_pool_set_enabled",
            {"profile_id": "worker", "enabled": "false"},
            "enabled must be a boolean",
        ),
        (
            set_pool_enabled,
            "_cmd_pool_set_enabled",
            {"profile_id": "missing", "enabled": False},
            "no pool profile 'missing'",
        ),
        (set_pool_bounds, "_cmd_pool_scale", {}, "profile_id is required"),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "worker"},
            "nothing to change: pass min and/or max",
        ),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "missing", "min": 0},
            "no pool profile 'missing'",
        ),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "worker", "min": -1},
            "min must be >= 0",
        ),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "worker", "min": None},
            "min must be >= 0",
        ),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "worker", "max": 0},
            "max must be >= 1",
        ),
        (
            set_pool_bounds,
            "_cmd_pool_scale",
            {"profile_id": "worker", "min": 4},
            "max must be >= min",
        ),
    ],
)
async def test_validation_matches_commands_and_leaves_state_untouched(
    handler,
    tmp_path,
    helper,
    command,
    args,
    error,
):
    before = await handler.db.get_profile("worker")
    path = tmp_path / "data/vault/agent-types/worker/profile.md"
    markdown = path.read_text(encoding="utf-8")
    events = collect_events(handler)
    expected = {"success": False, "error": error}
    assert await helper(handler, args, correlation={"request_id": "invalid"}) == expected
    assert await getattr(handler, command)(args) == expected
    assert await handler.db.get_profile("worker") == before
    assert path.read_text(encoding="utf-8") == markdown
    assert events == []


async def test_swarm_disabled_validation_matches_lifecycle_command(handler):
    handler.config.swarm.enabled = False
    args = {"profile_id": "worker", "lifecycle": "pool"}
    expected = {
        "success": False,
        "error": "cannot set lifecycle to pool while swarm.enabled is false",
    }
    assert await set_pool_lifecycle(handler, args) == expected
    assert await handler._cmd_pool_set_lifecycle(args) == expected


@pytest.mark.parametrize(
    "helper,command,args",
    [
        (set_pool_enabled, "_cmd_pool_set_enabled", {"profile_id": "worker", "enabled": False}),
        (set_pool_bounds, "_cmd_pool_scale", {"profile_id": "worker", "max": 4}),
    ],
)
async def test_pool_only_edits_refuse_task_profiles(handler, helper, command, args):
    await set_pool_lifecycle(handler, {"profile_id": "worker", "lifecycle": "task"})
    expected = {"success": False, "error": "no pool profile 'worker'"}
    assert await helper(handler, args) == expected
    assert await getattr(handler, command)(args) == expected


async def test_uncorrelated_command_keeps_its_response_and_event_payloads(handler):
    events = collect_events(handler)
    result = await handler._cmd_pool_set_enabled({"profile_id": "worker", "enabled": False})
    assert result == {
        "success": True,
        "profile_id": "worker",
        "enabled": False,
        "warnings": [],
    }
    assert events == [
        {"project_id": project_id, "profile_id": "worker", "enabled": False}
        for project_id in ("a", "b")
    ]


async def test_correlation_does_not_override_event_domain_fields(handler):
    events = collect_events(handler)
    correlation = {"request_id": "allocation", "project_id": "wrong", "enabled": True}
    await set_pool_enabled(
        handler,
        {"profile_id": "worker", "enabled": False},
        correlation=correlation,
    )
    assert {event["project_id"] for event in events} == {"a", "b"}
    assert all(event["enabled"] is False for event in events)
    assert correlation == {
        "request_id": "allocation",
        "project_id": "wrong",
        "enabled": True,
    }


async def test_vault_write_failure_retains_db_first_command_behavior(
    handler,
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "data/vault/agent-types/worker/profile.md"
    markdown = path.read_text(encoding="utf-8")
    write_text = Path.write_text

    def refuse_profile_write(target, *args, **kwargs):
        if target == path:
            raise PermissionError("read-only vault")
        return write_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_profile_write)
    result = await set_pool_bounds(handler, {"profile_id": "worker", "max": 7})
    assert result["success"] is True
    assert (result["before"]["max_active"], result["after"]["max_active"]) == (3, 7)
    assert (await handler.db.get_profile("worker")).max_active == 7
    assert path.read_text(encoding="utf-8") == markdown
    assert "changes applied to the agent_profiles row only" in caplog.text


async def test_rename_preserves_profile_routes_assignments_and_audits_backup(handler):
    path = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    original = path.read_bytes()
    before = await handler.db.get_profile("worker")
    await handler.db.create_task(Task(
        id="held", project_id="a", title="Held", description="Work",
        status=TaskStatus.IN_PROGRESS, profile_id="worker", route_source="override",
    ))
    held = await session(handler, "held-session", task_id="held")
    events = []
    handler.orchestrator.bus.subscribe("pool.renamed", lambda payload: events.append(payload))
    result = await handler._cmd_pool_rename({"profile_id": "worker", "name": " Space Bunny "})

    assert result["success"] and result["changed"]
    assert result["profile_id"] == "worker"
    assert (await handler.db.get_profile("worker")) == dataclasses.replace(before, name="Space Bunny")
    assert (await handler.db.get_task("held")).profile_id == "worker"
    assert (await handler.db.get_session(held.id)) == held
    assert Path(result["backup_path"]).read_bytes() == original
    renamed = parse_profile(path.read_text(encoding="utf-8"))
    assert renamed.frontmatter.name == "Space Bunny"
    assert renamed.config == parse_profile(original.decode()).config
    payload = {
        "profile_id": "worker", "old_name": "Worker", "name": "Space Bunny",
        "backup_path": result["backup_path"],
    }
    audit = await handler.db.get_recent_events(event_type="pool.renamed")
    assert len(audit) == 1 and json.loads(audit[0]["payload"]) == payload
    assert len(events) == 1 and events[0]["seq"] == audit[0]["id"]
    assert validate_payload("pool.renamed", payload) == []
    status = await handler._cmd_pool_status({})
    assert next(row for row in status["pools"] if row["profile_id"] == "worker")["name"] == "Space Bunny"
    profiles = await handler._cmd_list_profiles({})
    assert next(row for row in profiles["profiles"] if row["id"] == "worker")["name"] == "Space Bunny"


async def test_rename_keeps_derived_stub_and_survives_vault_resync(handler):
    root = Path(handler.config.data_dir) / "vault/agent-types"
    path = root / "worker/profile.md"
    template = root / "template/profile.md"
    template.parent.mkdir()
    template.write_bytes(path.read_bytes().replace(b"id: worker", b"id: template"))
    markdown = '''---
id: worker
name: Worker
extends: template
tags: [derived, operator]
custom: keep
---
# Authored title
## Config
```json
{"default_class": "standard-high", "lifecycle": "pool", "min_active": 2, "max_active": 7}
```
## Operator notes
Keep this section verbatim.
'''
    path.write_text(markdown, encoding="utf-8")
    assert (await sync_profile_text_to_db(markdown, handler.db, source_path=str(path))).success
    before = await handler.db.get_profile("worker")
    result = await handler._cmd_pool_rename({
        "profile_id": "worker", "name": 'OpenCode · Space Bunny: [High] "\\pilot"',
    })
    assert result["success"], result
    written = path.read_text(encoding="utf-8")
    assert written.split("---", 2)[2] == markdown.split("---", 2)[2]
    parsed = parse_profile(written)
    assert parsed.frontmatter.extends == "template"
    assert parsed.frontmatter.name == result["name"]
    assert (await handler.db.get_profile("worker")) == dataclasses.replace(before, name=result["name"])
    assert (await sync_profile_text_to_db(written, handler.db, source_path=str(path))).success
    assert (await handler.db.get_profile("worker")) == dataclasses.replace(before, name=result["name"])


@pytest.mark.parametrize("name", [None, 123, "", "  ", "x" * 121, "hello\nworld", "hi\x7f"])
async def test_invalid_rename_writes_nothing(handler, name):
    path = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    before = await handler.db.get_profile("worker")
    original = path.read_bytes()
    result = await handler._cmd_pool_rename({"profile_id": "worker", "name": name})
    assert result["success"] is False
    assert path.read_bytes() == original
    assert (await handler.db.get_profile("worker")) == before
    assert list(path.parent.glob("*.bak-*")) == []
    assert await handler.db.get_recent_events(event_type="pool.renamed") == []


async def test_rename_requires_existing_global_pool_and_name(handler):
    for profile_id in (None, "", "missing", "../worker", "project:a:worker"):
        result = await handler._cmd_pool_rename({"profile_id": profile_id, "name": "New"})
        assert result["success"] is False
    await set_pool_lifecycle(handler, {"profile_id": "worker", "lifecycle": "task"})
    assert (await handler._cmd_pool_rename({"profile_id": "worker", "name": "New"}))["success"] is False


async def test_rename_database_only_profile_is_backed_up_and_unchanged_name_is_noop(handler):
    path = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    path.unlink()
    before = await handler.db.get_profile("worker")
    result = await handler._cmd_pool_rename({"profile_id": "worker", "name": "x" * 120})
    assert result["success"]
    assert json.loads(Path(result["backup_path"]).read_text()) == dataclasses.asdict(before)
    assert not path.exists()
    repeated = await handler._cmd_pool_rename({"profile_id": "worker", "name": "x" * 120})
    assert repeated["success"] and not repeated["changed"]
    assert len(await handler.db.get_recent_events(event_type="pool.renamed")) == 1


@pytest.mark.parametrize("failure", ["backup", "write", "audit"])
async def test_rename_failure_keeps_original_name_and_source(handler, monkeypatch, failure):
    from src.profiles import drift

    path = Path(handler.config.data_dir) / "vault/agent-types/worker/profile.md"
    before = await handler.db.get_profile("worker")
    original = path.read_bytes()
    real_write = drift._atomic_write_bytes

    def refuse_write(target, data, **kwargs):
        if (failure == "backup" and ".bak-" in target) or (
            failure == "write" and target == str(path)
        ):
            raise PermissionError("read-only vault")
        return real_write(target, data, **kwargs)

    if failure == "audit":
        monkeypatch.setattr(handler.db, "log_event", AsyncMock(side_effect=RuntimeError("audit down")))
    else:
        monkeypatch.setattr(drift, "_atomic_write_bytes", refuse_write)
    result = await handler._cmd_pool_rename({"profile_id": "worker", "name": "New"})
    assert result["success"] is False
    assert (await handler.db.get_profile("worker")) == before
    assert path.read_bytes() == original
    assert await handler.db.get_recent_events(event_type="pool.renamed") == []


@pytest.mark.parametrize(
    "event_type",
    [
        "pool.lifecycle_changed",
        "pool.bounds_changed",
        "pool.session_drained",
        "pool.enabled_changed",
    ],
)
def test_pool_event_correlation_is_optional(event_type):
    schema = get_schema(event_type)
    assert "request_id" in schema["optional"]
    payload = dict.fromkeys(schema["required"], "value")
    assert validate_payload(event_type, payload) == []
    assert validate_payload(event_type, {**payload, "request_id": "allocation"}) == []
