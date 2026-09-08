from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.config import PlaybooksConfig
from src.database import Database
from src.database.tables import playbook_pending_events
from src.commands.principal import ExecutionPrincipal, principal_context
from tests.test_api_playbook_v2_commands import _backend_fixture
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def handler(tmp_path):
    db = Database(lease_dsn("delete.db"))
    await db.initialize()
    _, ref, _ = _backend_fixture()
    await db.upsert_playbook_artifact(ref, scope="system", scope_identifier="",
                                     path="/artifact.json", size_bytes=1)
    for scope, identifier, enabled in [("system", "", True), ("project", "old", False)]:
        await db.set_playbook_activation(
            playbook_id=ref.playbook_id, scope=scope, scope_identifier=identifier,
            artifact_sha256=ref.artifact_sha256, enabled=enabled, health="ready",
            reasons="[]", activated_by="test",
        )
    instance = PlaybookV2CommandsMixin()
    instance.db = db
    instance.config = SimpleNamespace(playbooks=PlaybooksConfig(enabled=True))
    yield instance, ref
    await db.close()


async def test_delete_removes_only_exact_disabled_scope(handler):
    command, ref = handler
    result = await command._cmd_playbook_delete(dict(
        playbook_id=ref.playbook_id, scope="project", scope_identifier="old",
        artifact_sha256=ref.artifact_sha256,
    ))
    assert result["deleted"] is True
    rows = await command.db.list_playbook_activations(enabled_only=False)
    assert [(r["scope"], r["enabled"]) for r in rows] == [("system", True)]


@pytest.mark.parametrize("mode", ["enabled", "stale_hash"])
async def test_delete_refuses_enabled_or_stale_target(handler, mode):
    command, ref = handler
    result = await command._cmd_playbook_delete(dict(
        playbook_id=ref.playbook_id,
        scope="system" if mode == "enabled" else "project",
        scope_identifier="" if mode == "enabled" else "old",
        artifact_sha256=ref.artifact_sha256 if mode == "enabled" else "sha256:" + "f" * 64,
    ))
    assert "error" in result
    assert len(await command.db.list_playbook_activations(enabled_only=False)) == 2


async def test_delete_refuses_unfinished_event(handler):
    command, ref = handler
    async with command.db.immediate() as conn:
        await conn.execute(insert(playbook_pending_events).values(
            pending_event_id="pending", playbook_id=ref.playbook_id, scope="project",
            scope_identifier="old", event_type="task.completed", reason="disabled",
            received_at=1.0, expires_at=9999999999.0,
        ))
    result = await command._cmd_playbook_delete(dict(
        playbook_id=ref.playbook_id, scope="project", scope_identifier="old",
        artifact_sha256=ref.artifact_sha256,
    ))
    assert "unfinished work" in result["error"]
    assert len(await command.db.list_playbook_activations(enabled_only=False)) == 2


async def test_delete_is_local_operator_only(handler):
    command, ref = handler
    with principal_context(ExecutionPrincipal.service("not-an-operator")):
        result = await command._cmd_playbook_delete(dict(
            playbook_id=ref.playbook_id, scope="project", scope_identifier="old",
            artifact_sha256=ref.artifact_sha256,
        ))
    assert "local operator" in result["error"]
    assert len(await command.db.list_playbook_activations(enabled_only=False)) == 2
