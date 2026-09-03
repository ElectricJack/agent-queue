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


async def test_orchestrator_trigger_constrains_v2_dispatch_to_the_admitted_playbook():
    """The manager admits one playbook per callback; the engine must not
    widen that to every scope-matching activation (amber-forge-10)."""
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
            SimpleNamespace(id="playbook-a", to_dict=dict),
            {"type": "task.completed", "event_id": "event-1"},
        )
        await __import__("asyncio").sleep(0)

    engine.dispatch_event.assert_awaited_once()
    assert engine.dispatch_event.await_args.kwargs["playbook_ids"] == {"playbook-a"}


def _v2_ref(playbook_id: str) -> ArtifactRef:
    from src.playbooks.definition import PlaybookDefinition
    from tests.playbook_v2_engine_helpers import artifact_ref_for, minimal_artifact

    payload = minimal_artifact().model_dump(mode="json")
    payload["id"] = playbook_id
    return artifact_ref_for(PlaybookDefinition.model_validate(payload))


def _recording_engine(refs):
    """A real ``PlaybookEngine`` over stub activations whose ``run_rule`` records
    ``(playbook_id, event_type)`` instead of walking the graph."""
    from src.playbooks.definition import PlaybookDefinition
    from src.playbooks.engine import PlaybookEngine, RunOutcome
    from src.playbooks.executors.base import EngineServices
    from src.playbooks.run_state import RunLifecycle
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        StubActivations,
        minimal_artifact,
    )

    store = InMemoryArtifactStore()
    for ref in refs:
        payload = minimal_artifact().model_dump(mode="json")
        payload["id"] = ref.playbook_id
        store.put(PlaybookDefinition.model_validate(payload))
    engine = PlaybookEngine(
        services=EngineServices(contracts=MagicMock(), clock=lambda: 0.0, artifact_store=store),
        activations=StubActivations(refs),
    )
    engine.started: list[tuple[str, str]] = []

    async def run_rule(ref, rule_id, event, principal, *, mode, dispatch_id):
        engine.started.append((ref.playbook_id, engine._event_type(event)))
        return RunOutcome(run_id=f"run-{ref.playbook_id}", lifecycle=RunLifecycle.COMPLETED, outcome="completed")

    engine.run_rule = run_rule
    return engine


async def test_engine_dispatch_only_runs_the_admitted_playbooks():
    from src.commands.principal import ExecutionPrincipal

    engine = _recording_engine([_v2_ref("playbook-a"), _v2_ref("playbook-b")])
    principal = ExecutionPrincipal.service("test")
    event = {"type": "task.completed", "event_id": "e-1"}

    result = await engine.dispatch_event(event, principal, playbook_ids={"playbook-b"})
    assert engine.started == [("playbook-b", "task.completed")]
    assert result.run_ids == ("run-playbook-b",)

    engine.started.clear()
    await engine.dispatch_event(event, principal)
    assert sorted(engine.started) == [
        ("playbook-a", "task.completed"),
        ("playbook-b", "task.completed"),
    ]


def _v2_playbook(playbook_id: str, **fields):
    from src.playbooks.models import CompiledPlaybook

    return CompiledPlaybook(
        id=playbook_id,
        version=1,
        source_hash="sha256:" + "1" * 64,
        triggers=["task.completed"],
        scope=fields.pop("scope", "system"),
        **fields,
    )


async def _manager_env(tmp_path, playbooks, *, max_concurrent_runs: int = 2):
    """Real PlaybookManager + EventBus wired to the production V2 trigger
    callback, with the recording engine standing in for the built one.

    Returns ``(manager, engine, admitted, emit)`` where ``admitted`` records
    every playbook the manager's admission passed to the callback and
    ``emit`` publishes one ``task.completed`` and lets the dispatch settle.
    """
    import asyncio

    from src.event_bus import EventBus
    from src.orchestrator.core import Orchestrator
    from src.playbooks.manager import PlaybookManager

    bus = EventBus()
    manager = PlaybookManager(
        config=SimpleNamespace(data_dir=str(tmp_path), vault_root=None),
        event_bus=bus,
        data_dir=str(tmp_path),
        max_concurrent_runs=max_concurrent_runs,
    )
    engine = _recording_engine([_v2_ref(playbook.id) for playbook in playbooks])
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(playbooks=PlaybooksConfig(enabled=True, v2_engine=True)),
        db=MagicMock(),
        _command_handler=MagicMock(),
        llm=MagicMock(),
        bus=bus,
    )
    real_callback = Orchestrator._on_playbook_trigger.__get__(orchestrator, Orchestrator)
    admitted: list[str] = []

    async def spy(playbook, data):
        admitted.append(playbook.id)
        await real_callback(playbook, data)

    manager.on_trigger = spy
    for playbook in playbooks:
        await manager.install_compiled(playbook)
    assert manager.subscribe_to_events() == len(playbooks)

    async def emit(event_id: str, **data) -> None:
        with patch("src.playbooks.services.build_v2_engine", return_value=engine):
            await bus.emit("task.completed", {"event_id": event_id, **data})
            for _ in range(20):
                await asyncio.sleep(0)

    return manager, engine, admitted, emit


async def test_manager_cooldown_governs_v2_runs_end_to_end(tmp_path):
    """The task's reproduction: b on a 3600s cooldown, the manager admits only
    a, and the V2 engine must start only a."""
    manager, engine, admitted, emit = await _manager_env(
        tmp_path,
        [
            _v2_playbook("playbook-a", cooldown_seconds=3600),
            _v2_playbook("playbook-b", cooldown_seconds=3600),
        ],
    )
    manager.record_execution("playbook-b", "system")

    await emit("sweep-1")
    assert admitted == ["playbook-a"]
    assert engine.started == [("playbook-a", "task.completed")]

    # Both on cooldown now that a ran: nothing admitted, nothing started.
    manager.record_execution("playbook-a", "system")
    engine.started.clear()
    admitted.clear()
    await emit("sweep-2")
    assert admitted == []
    assert engine.started == []


async def test_manager_shadowing_governs_v2_runs_end_to_end(tmp_path):
    """A project pipeline shadows the system pipeline of the same role for
    that project's events (spec §4.5); the engine must honour the same cut."""
    manager, engine, admitted, emit = await _manager_env(
        tmp_path,
        [
            _v2_playbook("system-pipeline", kind="pipeline", role="default-pipeline"),
            _v2_playbook(
                "project-pipeline", kind="pipeline", role="default-pipeline", scope="project"
            ),
        ],
    )
    manager.set_scope_identifier("project-pipeline", "proj")

    await emit("created-1", project_id="proj")
    assert admitted == ["project-pipeline"]
    assert engine.started == [("project-pipeline", "task.completed")]

    # Another project's event: no shadowing there, only the system copy matches.
    engine.started.clear()
    admitted.clear()
    await emit("created-2", project_id="other")
    assert admitted == ["system-pipeline"]
    assert engine.started == [("system-pipeline", "task.completed")]


async def test_manager_concurrency_cap_governs_v2_runs_end_to_end(tmp_path):
    """With the global cap full the manager admits nothing, so the engine must
    start nothing — not every ready activation."""
    import asyncio

    manager, engine, admitted, emit = await _manager_env(
        tmp_path,
        [_v2_playbook("playbook-a"), _v2_playbook("playbook-b")],
        max_concurrent_runs=1,
    )
    blocker = asyncio.get_running_loop().create_future()
    holder = asyncio.ensure_future(blocker)
    try:
        assert manager.register_run("run-x", "playbook-a", holder)
        assert not manager.can_start_run()

        await emit("sweep-1")
        assert admitted == []
        assert engine.started == []
    finally:
        blocker.cancel()
