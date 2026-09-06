import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.handler import CommandHandler
from src.commands.playbook_commands import PlaybookCommandsMixin
from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import source_digest
from src.playbooks.profiles import shipped_profile_lookup
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup

SOURCE = (
    Path("tests/fixtures/playbooks/v2/lowering/output-ref-no-loop.pipeline.md")
    .read_text(encoding="utf-8")
    .replace("id: output-ref-no-loop", "id: router")
)


class _Handler(PlaybookCommandsMixin, PlaybookV2CommandsMixin):
    def __init__(self, tmp_path) -> None:
        self.source_path = tmp_path / "vault" / "system" / "playbooks" / "router.md"
        self.source_path.parent.mkdir(parents=True)
        self.source_path.write_text(SOURCE, encoding="utf-8")
        self.db = SimpleNamespace(
            list_playbook_activations=AsyncMock(
                return_value=[
                    {
                        "playbook_id": "router",
                        "scope": "system",
                        "scope_identifier": "",
                        "active_artifact_sha256": None,
                        "enabled": True,
                        "health": "ready",
                    }
                ]
            ),
            upsert_playbook_artifact=AsyncMock(),
            set_playbook_activation=AsyncMock(),
            get_playbook_artifact_row=AsyncMock(return_value=None),
        )
        self.config = SimpleNamespace(
            data_dir=str(tmp_path),
            vault_root=str(tmp_path / "vault"),
            compiled_root=str(tmp_path / "compiled"),
            playbooks=SimpleNamespace(enabled=True, v2_max_artifact_bytes=1_048_576),
        )
        self.orchestrator = SimpleNamespace(llm=None, bus=None)
        self._store = ArtifactStore(self.config.compiled_root)

        @asynccontextmanager
        async def artifact_hash_lock(_shas):
            yield object()

        self.db.artifact_hash_lock = artifact_hash_lock

    def _v2_engine(self):
        return SimpleNamespace(services=SimpleNamespace(artifact_store=self._store))

    async def _v2_lookups(self):
        return RegistryContractLookup(), shipped_profile_lookup(), RegisteredEventLookup()


async def test_update_source_is_a_v2_command_that_compiles_and_activates(tmp_path) -> None:
    handler = _Handler(tmp_path)
    updated = SOURCE + "\nUpdated prose.\n"

    result = await handler._cmd_update_playbook_source(
        {
            "playbook_id": "router",
            "markdown": updated,
            "expected_source_hash": source_digest(SOURCE),
        }
    )

    assert result["compiled"] is True
    assert result["playbook_id"] == "router"
    assert result["version"] == 1
    assert result["node_count"] == 3
    assert result["triggers"] == ["task.completed"]
    assert handler.source_path.read_text(encoding="utf-8") == updated
    handler.db.upsert_playbook_artifact.assert_awaited_once()
    activation = handler.db.set_playbook_activation.await_args.kwargs
    assert activation["playbook_id"] == "router"
    assert activation["artifact_sha256"].startswith("sha256:")
    assert activation["health"] == "ready"


async def test_command_handler_dispatches_update_source_instead_of_returning_unknown(
    tmp_path,
) -> None:
    source_path = tmp_path / "vault" / "system" / "playbooks" / "router.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    db = SimpleNamespace(
        list_playbook_activations=AsyncMock(
            return_value=[
                {
                    "playbook_id": "router",
                    "scope": "system",
                    "scope_identifier": "",
                    "active_artifact_sha256": None,
                    "enabled": True,
                }
            ]
        )
    )
    config = SimpleNamespace(
        data_dir=str(tmp_path),
        vault_root=str(tmp_path / "vault"),
        playbooks=SimpleNamespace(enabled=True),
        memory=SimpleNamespace(enabled=True),
        security=SimpleNamespace(capability_enforcement="off"),
        events=None,
    )
    orchestrator = SimpleNamespace(db=db, plugin_registry=None, bus=None)
    handler = CommandHandler(orchestrator, config)

    result = await handler.execute(
        "update_playbook_source",
        {
            "playbook_id": "router",
            "markdown": SOURCE,
            "expected_source_hash": "sha256:" + "0" * 64,
        },
    )

    assert result["error"] == "conflict"
    assert "Unknown command" not in result["error"]


async def test_update_source_conflict_does_not_write_or_compile(tmp_path) -> None:
    handler = _Handler(tmp_path)

    result = await handler._cmd_update_playbook_source(
        {
            "playbook_id": "router",
            "markdown": SOURCE + "\nChanged.\n",
            "expected_source_hash": "sha256:" + "0" * 64,
        }
    )

    assert result["error"] == "conflict"
    assert result["current_source_hash"] == source_digest(SOURCE)
    assert handler.source_path.read_text(encoding="utf-8") == SOURCE
    handler.db.upsert_playbook_artifact.assert_not_awaited()
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_update_prose_source_keeps_previous_activation_when_not_lowerable(tmp_path) -> None:
    handler = _Handler(tmp_path)
    prose = SOURCE.replace("kind: pipeline\n", "") + "\nDescribe a new workflow.\n"

    result = await handler._cmd_update_playbook_source(
        {
            "playbook_id": "router",
            "markdown": prose,
        }
    )

    assert result["compiled"] is False
    assert "compiler-agent proposal" in result["errors"][0]
    assert handler.source_path.read_text(encoding="utf-8") == prose
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_update_source_rejects_scope_change_before_persisting_artifact(tmp_path) -> None:
    handler = _Handler(tmp_path)
    changed_scope = SOURCE.replace("scope: system", "scope: project:other")

    result = await handler._cmd_update_playbook_source(
        {"playbook_id": "router", "markdown": changed_scope}
    )

    assert result["compiled"] is False
    assert "scope" in result["errors"][0]
    assert handler.source_path.read_text(encoding="utf-8") == changed_scope
    handler.db.upsert_playbook_artifact.assert_not_awaited()
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_update_source_preserves_disabled_activation_state(tmp_path) -> None:
    handler = _Handler(tmp_path)
    handler.db.list_playbook_activations.return_value[0]["enabled"] = False

    result = await handler._cmd_update_playbook_source(
        {"playbook_id": "router", "markdown": SOURCE + "\nUpdated.\n"}
    )

    assert result["compiled"] is True
    activation = handler.db.set_playbook_activation.await_args.kwargs
    assert activation["enabled"] is False
    assert activation["health"] == "disabled"


async def test_update_source_lowers_a_pipeline_machine_block(tmp_path) -> None:
    handler = _Handler(tmp_path)
    pipeline = (
        Path("tests/fixtures/playbooks/v2/lowering/output-ref-no-loop.pipeline.md")
        .read_text(encoding="utf-8")
        .replace("id: output-ref-no-loop", "id: router")
    )

    result = await handler._cmd_update_playbook_source(
        {"playbook_id": "router", "markdown": pipeline}
    )

    assert result["compiled"] is True
    assert result["node_count"] == 3
    assert result["triggers"] == ["task.completed"]


async def test_update_source_activation_failure_keeps_persisted_artifact_inactive(
    tmp_path,
) -> None:
    handler = _Handler(tmp_path)
    handler.db.set_playbook_activation.side_effect = RuntimeError("database unavailable")

    result = await handler._cmd_update_playbook_source(
        {"playbook_id": "router", "markdown": SOURCE + "\nUpdated.\n"}
    )

    assert result["compiled"] is False
    assert result["errors"] == ["V2 activation failed: database unavailable"]
    assert len(list((tmp_path / "compiled" / "artifacts").glob("*.json"))) == 1
    handler.db.upsert_playbook_artifact.assert_awaited_once()


async def test_update_source_cancellation_during_activation_propagates_with_artifact_persisted(
    tmp_path,
) -> None:
    handler = _Handler(tmp_path)
    handler.db.set_playbook_activation.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handler._cmd_update_playbook_source(
            {"playbook_id": "router", "markdown": SOURCE + "\nUpdated.\n"}
        )

    assert len(list((tmp_path / "compiled" / "artifacts").glob("*.json"))) == 1
    handler.db.upsert_playbook_artifact.assert_awaited_once()


async def test_update_source_cancellation_during_artifact_row_write_removes_new_file(
    tmp_path,
) -> None:
    handler = _Handler(tmp_path)
    handler.db.upsert_playbook_artifact.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handler._cmd_update_playbook_source(
            {"playbook_id": "router", "markdown": SOURCE + "\nUpdated.\n"}
        )

    assert list((tmp_path / "compiled" / "artifacts").glob("*.json")) == []
    handler.db.set_playbook_activation.assert_not_awaited()
