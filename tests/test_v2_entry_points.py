from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import PlaybooksConfig
from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION, ArtifactRef
from src.playbooks.services import (
    DatabaseActivationSource,
    build_v2_engine,
    v2_engine_enabled,
)


def test_v2_engine_flag_requires_the_playbook_subsystem():
    assert not v2_engine_enabled(SimpleNamespace(playbooks=PlaybooksConfig()))
    assert not v2_engine_enabled(
        SimpleNamespace(playbooks=PlaybooksConfig(enabled=False, v2_engine=True))
    )
    assert v2_engine_enabled(
        SimpleNamespace(playbooks=PlaybooksConfig(enabled=True, v2_engine=True))
    )


def test_v2_dry_run_bounds_are_positive():
    errors = PlaybooksConfig(v2_dry_run_max_paths=0, v2_dry_run_max_step_visits=0).validate()
    assert {error.field for error in errors} == {
        "v2_dry_run_max_paths",
        "v2_dry_run_max_step_visits",
    }


def test_v2_engine_defaults_are_the_locked_values():
    config = PlaybooksConfig()
    assert config.v2_engine is False
    assert config.v2_dry_run_max_paths == 32
    assert config.v2_dry_run_max_step_visits == 1000


async def test_activation_source_returns_only_enabled_ready_artifacts():
    wanted = ArtifactRef(
        playbook_id="ready",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=ARTIFACT_SCHEMA_GENERATION,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test",
    )

    class Database:
        async def list_playbook_activations(self, *, enabled_only=False):
            assert enabled_only is True
            return [
                {"enabled": True, "scope": "system", "health": "ready", "active_artifact_sha256": wanted.artifact_sha256},
                {"enabled": True, "scope": "system", "health": "stale_contract", "active_artifact_sha256": "sha256:" + "d" * 64},
            ]

        async def get_playbook_artifact(self, artifact_sha256):
            return wanted if artifact_sha256 == wanted.artifact_sha256 else None

    assert await DatabaseActivationSource(Database()).ready_activations("task.completed") == [wanted]


async def test_activation_source_enforces_event_scope():
    refs = {
        suffix: ArtifactRef(
            playbook_id=suffix,
            artifact_sha256="sha256:" + suffix * 64,
            schema_generation=ARTIFACT_SCHEMA_GENERATION,
            contract_fingerprint="sha256:" + "b" * 64,
            source_digest="sha256:" + "c" * 64,
            compiler_build="test",
        )
        for suffix in ("a", "b", "c", "d")
    }

    class Database:
        async def list_playbook_activations(self, *, enabled_only=False):
            return [
                {"scope": "system", "scope_identifier": "", "health": "ready", "active_artifact_sha256": refs["a"].artifact_sha256},
                {"scope": "project", "scope_identifier": "project-a", "health": "ready", "active_artifact_sha256": refs["b"].artifact_sha256},
                {"scope": "project", "scope_identifier": "project-b", "health": "ready", "active_artifact_sha256": refs["c"].artifact_sha256},
                {"scope": "agent_type", "scope_identifier": "coding", "health": "ready", "active_artifact_sha256": refs["d"].artifact_sha256},
            ]

        async def get_playbook_artifact(self, artifact_sha256):
            return next(ref for ref in refs.values() if ref.artifact_sha256 == artifact_sha256)

    selected = await DatabaseActivationSource(Database()).ready_activations(
        "task.completed", {"project_id": "project-a", "agent_type": "coding"}
    )
    assert selected == [refs["a"], refs["b"], refs["d"]]


def test_production_engine_is_shared_by_all_handler_entry_points():
    handler = SimpleNamespace()
    config = SimpleNamespace(
        compiled_root="/tmp/compiled",
        security=SimpleNamespace(capability_enforcement="audit"),
        playbooks=SimpleNamespace(
            v2_max_artifact_bytes=1024,
            v2_dry_run_max_step_visits=1000,
            cancellation_grace_seconds=30,
        ),
    )
    db = MagicMock()
    with patch("src.playbooks.engine.PlaybookEngine") as engine_class:
        first = build_v2_engine(config=config, db=db, handler=handler)
        second = build_v2_engine(config=config, db=db, handler=handler)

    assert first is second
    engine_class.assert_called_once()
    assert "max_step_visits" not in engine_class.call_args.kwargs


async def test_orchestrator_trigger_reaches_v2_engine_when_enabled():
    from src.orchestrator.core import Orchestrator

    engine = MagicMock()
    engine.dispatch_event = AsyncMock()
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(playbooks=PlaybooksConfig(enabled=True, v2_engine=True)),
        db=MagicMock(),
        _command_handler=MagicMock(),
        llm=MagicMock(),
        bus=MagicMock(),
    )
    with patch("src.playbooks.services.build_v2_engine", return_value=engine):
        await Orchestrator._on_playbook_trigger(
            orchestrator,
            SimpleNamespace(id="playbook", to_dict=lambda: {}),
            {"type": "task.completed", "event_id": "event-1"},
        )
        await __import__("asyncio").sleep(0)

    engine.dispatch_event.assert_awaited_once()
