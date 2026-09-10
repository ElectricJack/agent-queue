"""Durable dashboard-state contract: storage, policy, API, and events."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import insert, select

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.api.websocket import WebSocketManager
from src.commands.handler import CommandHandler
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import AppConfig, DatabaseConfig
from src.dashboard_state.namespaces import NAMESPACES
from src.database import Database
from src.database.tables import dashboard_state_documents
from src.doctor.dashboard_state_checks import _check_orphans, _fix_orphans
from src.doctor.models import DoctorContext, Severity
from src.event_bus import EventBus
from src.models import Project
from src.orchestrator import Orchestrator
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def env(tmp_path):
    dsn = lease_dsn("dashboard-state")
    db = Database(dsn)
    await db.initialize()
    await db.create_project(Project(id="p1", name="One"))
    await db.create_project(Project(id="p2", name="Two"))
    config = AppConfig(
        database=DatabaseConfig(url=dsn),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus = EventBus(env="dev")
    yield CommandHandler(orchestrator, config), db, orchestrator.bus, config
    await db.close()


def test_registry_models_and_defaults_are_strict_and_complete() -> None:
    from src.api.models.dashboard import Document, Namespace
    from typing import get_args

    assert set(get_args(Namespace)) == set(NAMESPACES)
    union = get_args(Document)[0]
    document_names = {
        get_args(model.model_fields["namespace"].annotation)[0] for model in get_args(union)
    }
    assert document_names == set(NAMESPACES)
    for spec in NAMESPACES.values():
        assert spec.model.model_config["extra"] == "forbid"
        assert spec.model.model_validate(spec.default_value())

    from src.database.legacy_sqlite_import import _ORDERED_TABLES

    assert dashboard_state_documents in _ORDERED_TABLES


async def test_bootstrap_synthesizes_all_global_defaults(env) -> None:
    handler, _db, _bus, _config = env
    result = await handler.execute("dashboard_state_list", {})
    assert result["success"] is True
    assert result["owner_id"] == "human:local-operator"
    assert {item["namespace"] for item in result["documents"]} == {
        name for name, spec in NAMESPACES.items() if spec.subject == "none"
    }
    assert all(item["revision"] == 0 and not item["exists"] for item in result["documents"])


async def test_write_conflict_and_reset_preserve_monotonic_revision(env) -> None:
    handler, db, _bus, _config = env
    written = await handler.execute(
        "dashboard_state_put",
        {
            "namespace": "nav_organization",
            "base_revision": 0,
            "value": {"folders": [], "assignments": {}, "project_order": ["p1"]},
        },
    )
    assert written["document"]["revision"] == 1
    stale = await handler.execute(
        "dashboard_state_put",
        {
            "namespace": "nav_organization",
            "base_revision": 0,
            "value": {"folders": [], "assignments": {}, "project_order": ["p2"]},
        },
    )
    assert stale["error_code"] == "revision_conflict"
    assert stale["current"] == written["document"]

    reset = await handler.execute("dashboard_state_reset", {"namespace": "nav_organization"})
    assert reset["document"]["revision"] == 2
    assert reset["document"]["exists"] is False
    assert reset["document"]["value"] == NAMESPACES["nav_organization"].default_value()
    row = await db.get_dashboard_document(
        scope="workspace", owner_id="", namespace="nav_organization", subject=""
    )
    assert row is not None and row["value"] is None


async def test_lww_and_project_keyed_documents_are_independent(env) -> None:
    handler, _db, _bus, _config = env
    first = await handler.execute(
        "dashboard_state_put",
        {"namespace": "shell_preferences", "value": {"theme": "light"}},
    )
    second = await handler.execute(
        "dashboard_state_put",
        {"namespace": "shell_preferences", "value": {"theme": "system"}},
    )
    assert second["document"]["revision"] == first["document"]["revision"] + 1
    assert second["document"]["value"]["theme"] == "system"

    for project in ("p1", "p2"):
        result = await handler.execute(
            "dashboard_state_put",
            {
                "namespace": "command_center_project_view",
                "subject": project,
                "base_revision": 0,
                "value": {"expanded_task_ids": [project]},
            },
        )
        assert result["document"]["subject"] == project
        assert result["document"]["revision"] == 1


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"namespace": "missing"}, "unknown_namespace"),
        ({"namespace": "nav_organization", "subject": "p1"}, "subject_not_allowed"),
        ({"namespace": "command_center_project_view"}, "subject_required"),
        (
            {"namespace": "command_center_project_view", "subject": "missing"},
            "unknown_subject",
        ),
    ],
)
async def test_address_validation(env, args, code) -> None:
    handler, _db, _bus, _config = env
    result = await handler.execute("dashboard_state_get", args)
    assert result["error_code"] == code


async def test_value_validation_cas_requirement_and_size_limit(env) -> None:
    handler, _db, _bus, _config = env
    missing_base = await handler.execute(
        "dashboard_state_put",
        {"namespace": "nav_organization", "value": {}},
    )
    assert missing_base["error_code"] == "base_revision_required"

    invalid = await handler.execute(
        "dashboard_state_put",
        {
            "namespace": "nav_organization",
            "base_revision": 0,
            "value": {"folders": [], "assignments": {"p1": "missing"}},
        },
    )
    assert invalid["error_code"] == "invalid_value"
    assert invalid["errors"]

    too_large = await handler.execute(
        "dashboard_state_put",
        {
            "namespace": "shell_preferences",
            "value": {
                "right_surface": {
                    "kind": "pane",
                    "pane": {"view": "x", "args": {"blob": "x" * (64 * 1024)}},
                }
            },
        },
    )
    assert too_large["error_code"] == "value_too_large"


async def test_same_revision_concurrent_cas_has_one_winner(env) -> None:
    handler, _db, _bus, _config = env

    async def write(project_id: str, base_revision: int):
        return await handler.execute(
            "dashboard_state_put",
            {
                "namespace": "nav_organization",
                "base_revision": base_revision,
                "value": {"project_order": [project_id]},
            },
        )

    absent = await asyncio.gather(write("p1", 0), write("p2", 0))
    assert sorted(item.get("success", False) for item in absent) == [False, True]
    winner = next(item for item in absent if item.get("success"))
    present = await asyncio.gather(write("p1", 1), write("p2", 1))
    assert sorted(item.get("success", False) for item in present) == [False, True]
    new_winner = next(item for item in present if item.get("success"))
    assert winner["document"]["revision"] == 1
    assert new_winner["document"]["revision"] == 2


async def test_user_rows_are_isolated_by_server_supplied_owner(env) -> None:
    _handler, db, _bus, _config = env
    for owner, theme in (("human:a", "light"), ("human:b", "system")):
        row, conflict = await db.write_dashboard_document(
            scope="user",
            owner_id=owner,
            namespace="shell_preferences",
            subject="",
            value={"theme": theme},
            base_revision=None,
            now=1.0,
        )
        assert row is not None and conflict is None
    a = await db.list_dashboard_documents(owner_id="human:a")
    b = await db.list_dashboard_documents(owner_id="human:b")
    assert [row["value"]["theme"] for row in a] == ["light"]
    assert [row["value"]["theme"] for row in b] == ["system"]


@pytest.mark.parametrize(
    "kind", [PrincipalKind.SESSION, PrincipalKind.PLAYBOOK, PrincipalKind.SERVICE]
)
async def test_non_human_principals_are_refused(env, kind) -> None:
    handler, _db, _bus, _config = env
    principal = ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)
    with principal_context(principal):
        result = await handler._cmd_dashboard_state_list({})
    assert result["error_code"] == "human_required"


def test_session_token_scope_never_admits_dashboard_commands() -> None:
    scope = RequestScope(kind="session", session_id="s", project_id="p", task_id="t")
    error = check_command_scope("dashboard_state_get", {}, scope)
    assert error == "out of scope: dashboard_state_get"


async def test_mutation_persists_redacted_event_and_emits_its_seq(env) -> None:
    handler, db, bus, _config = env
    seen = []
    bus.subscribe("dashboard_state.changed.v1", lambda payload: seen.append(dict(payload)))
    result = await handler.execute(
        "dashboard_state_put",
        {"namespace": "shell_preferences", "value": {"theme": "light"}},
    )
    assert result["success"] is True
    rows = await db.get_recent_events(event_type="dashboard_state.changed.v1")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert "value" not in payload
    assert seen[0]["seq"] == rows[0]["id"]
    assert seen[0]["revision"] == 1


async def test_websocket_filters_dashboard_events_to_humans_and_owner(env) -> None:
    _handler, _db, bus, _config = env
    manager = WebSocketManager(bus)
    manager._clients["local"] = asyncio.Queue()
    manager._client_scope["local"] = RequestScope(kind="local")
    manager._clients["session"] = asyncio.Queue()
    manager._client_scope["session"] = RequestScope(kind="session", session_id="s")
    manager.start()
    try:
        base = {
            "version": 1,
            "namespace": "shell_preferences",
            "subject": None,
            "revision": 1,
            "change": "write",
            "updated_at": 1.0,
        }
        await bus.emit(
            "dashboard_state.changed.v1",
            {**base, "scope": "user", "owner_id": "human:someone-else"},
        )
        assert manager._clients["local"].empty()
        await bus.emit(
            "dashboard_state.changed.v1",
            {**base, "scope": "user", "owner_id": "human:local-operator"},
        )
        assert not manager._clients["local"].empty()
        assert manager._clients["session"].empty()
    finally:
        manager.shutdown()


async def test_project_delete_reaps_documents_and_doctor_repairs_orphans(env) -> None:
    handler, db, _bus, config = env
    await handler.execute(
        "dashboard_state_put",
        {
            "namespace": "command_center_project_view",
            "subject": "p1",
            "base_revision": 0,
            "value": {},
        },
    )
    await db.delete_project("p1")
    assert (
        await db.get_dashboard_document(
            scope="user",
            owner_id="human:local-operator",
            namespace="command_center_project_view",
            subject="p1",
        )
        is None
    )

    async with db._engine.begin() as conn:
        await conn.execute(
            insert(dashboard_state_documents).values(
                scope="user",
                owner_id="human:local-operator",
                namespace="command_center_project_view",
                subject="gone",
                revision=1,
                value={},
                created_at=1.0,
                updated_at=1.0,
            )
        )
    ctx = DoctorContext(config=config, db=db)
    before = await _check_orphans(ctx)
    assert before.severity is Severity.WARN and before.fixable
    after = await _fix_orphans(ctx)
    assert after.severity is Severity.OK and after.fix_applied
    async with db._engine.begin() as conn:
        assert (await conn.execute(select(dashboard_state_documents))).first() is None


async def test_typed_routes_preserve_coded_errors_and_conflicts(env) -> None:
    handler, _db, _bus, config = env
    from src.api.app import create_app

    app = create_app(handler.orchestrator, config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unknown = await client.post(
            "/api/dashboard/state-get", json={"namespace": "not_registered"}
        )
        assert unknown.status_code == 422
        assert unknown.json()["error_code"] == "unknown_namespace"

        invalid = await client.post(
            "/api/dashboard/state-put",
            json={
                "namespace": "nav_organization",
                "base_revision": 0,
                "value": {"folders": [], "assignments": {"p1": "missing"}},
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error_code"] == "invalid_value"

        first = await client.post(
            "/api/dashboard/state-put",
            json={"namespace": "nav_organization", "base_revision": 0, "value": {}},
        )
        assert first.status_code == 200
        stale = await client.post(
            "/api/dashboard/state-put",
            json={"namespace": "nav_organization", "base_revision": 0, "value": {}},
        )
        assert stale.status_code == 409
        assert stale.json()["current"]["revision"] == 1
