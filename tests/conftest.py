"""Root-level test fixtures shared across all test modules."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import TaskContext  # noqa: F401  (re-exported for test modules)

# Fresh per-test SQLite databases use the migration template cache. Individual
# migration tests can request ``disable_schema_cache`` to exercise Alembic.
os.environ.setdefault("AQ_SCHEMA_CACHE", "1")


@pytest.fixture
def disable_schema_cache(monkeypatch):
    """Force a test through the real Alembic chain instead of the template."""
    monkeypatch.setenv("AQ_SCHEMA_CACHE", "0")


@pytest.fixture(scope="session")
def claude_cli_path() -> str:
    """Resolve the ``claude`` CLI binary. Skip if missing."""
    path = shutil.which("claude")
    if path is None:
        pytest.skip("claude CLI not found on PATH — skipping functional tests")
    return path


@pytest.fixture(scope="session")
def claude_cli_authenticated(claude_cli_path: str) -> str:
    """Verify the ``claude`` CLI is installed *and* authenticated.

    Exercises the path production actually uses: agents run as tmux sessions
    wrapping this CLI.  The previous version drove a ``ClaudeSDKRuntime``
    round-trip, but that runtime was deleted in the tmux-harness migration —
    and testing auth through a code path nobody runs was misleading even
    before it stopped compiling.

    Skips (never fails) when the CLI is missing, unauthenticated, or slow:
    this gates optional functional tests, and an unauthenticated dev machine
    is a normal state, not a broken build.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [claude_cli_path, "-p", "respond with only: ok"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"claude CLI auth check could not run: {exc}")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        pytest.skip(f"claude CLI not authenticated (rc={proc.returncode}): {detail}")

    return claude_cli_path


@pytest.fixture(scope="session")
def npm_available() -> str:
    """Resolve ``npx`` binary. Skip if missing."""
    path = shutil.which("npx")
    if path is None:
        pytest.skip("npx not found on PATH — skipping MCP functional tests")
    return path


# ---------------------------------------------------------------------------
# Plugin system fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_registry(tmp_path: Path):
    """Bare PluginRegistry with no plugins loaded.

    Backed by AsyncMock db / MagicMock bus / MagicMock config so tests can
    exercise registry behavior without spinning up real subsystems.
    """
    from src.plugins.registry import PluginRegistry

    db = AsyncMock()
    db.get_plugin = AsyncMock(return_value=None)
    db.create_plugin = AsyncMock()
    db.update_plugin = AsyncMock()
    db.delete_plugin = AsyncMock()
    db.list_plugins = AsyncMock(return_value=[])
    db.get_plugin_data = AsyncMock(return_value=None)
    db.set_plugin_data = AsyncMock()
    db.delete_plugin_data = AsyncMock()
    db.delete_plugin_data_all = AsyncMock()

    bus = MagicMock()
    bus.emit = AsyncMock()
    bus.subscribe = MagicMock()

    config = MagicMock()
    config.data_dir = str(tmp_path / "data")
    os.makedirs(config.data_dir, exist_ok=True)

    return PluginRegistry(db=db, bus=bus, config=config)


@pytest.fixture
def plugin_context_factory(tmp_path: Path):
    """Build a PluginContext with the given trust_level / services for unit tests."""
    from src.plugins.base import PluginContext, TrustLevel

    def _make(
        *,
        trust_level: TrustLevel = TrustLevel.EXTERNAL,
        services: dict | None = None,
        plugin_name: str = "testplugin",
    ):
        db = AsyncMock()
        bus = MagicMock()
        bus.emit = AsyncMock()
        bus.subscribe = MagicMock()
        return PluginContext(
            plugin_name=plugin_name,
            install_path=str(tmp_path / "install"),
            data_path=str(tmp_path / "data"),
            db=db,
            bus=bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            trust_level=trust_level,
            services=services or {},
        )

    return _make


@pytest.fixture
def plugin_registry_with_plugin(plugin_registry):
    """Helper that loads an in-memory plugin class into the registry.

    Usage::

        async def test_x(plugin_registry_with_plugin):
            registry = await plugin_registry_with_plugin(MyPluginCls)
            ...
    """

    async def _load(plugin_cls):
        await plugin_registry.register_in_memory_plugin(plugin_cls)
        return plugin_registry

    return _load


@pytest.fixture
async def internal_plugins_handler(tmp_path: Path):
    """Async factory: CommandHandler with real DB/EventBus and internal plugins loaded.

    Promoted from the module-local ``handler`` fixtures in
    ``tests/test_file_command_handlers.py`` / ``tests/test_git_command_handlers.py``
    (FU-13).  Builds a real ``Database``, a real ``EventBus``, a ``PluginRegistry``
    with ``build_internal_services`` + ``load_internal_plugins()``, and a
    ``CommandHandler`` wired with ``set_active_project_id_getter``.

    The git manager defaults to ``create_autospec(GitManager, instance=True)``
    (with a real ``slugify``) so mocked methods track the real ``GitManager``
    surface; callers may pass their own ``db`` / ``config`` / ``git``.

    The returned handler exposes ``_db``, ``_bus``, ``_git``, and
    ``_plugin_registry`` attributes for test assertions.
    """
    from unittest.mock import create_autospec

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.git.manager import GitManager
    from src.orchestrator import Orchestrator
    from src.plugins.registry import PluginRegistry
    from src.plugins.services import build_internal_services

    created_dbs: list = []

    async def _make(*, db=None, config=None, git=None):
        if config is None:
            config = AppConfig(
                discord=DiscordConfig(bot_token="test-token", guild_id="123"),
                workspace_dir=str(tmp_path / "workspaces"),
                database_path=str(tmp_path / "plugins-handler.db"),
                data_dir=str(tmp_path / "data"),
            )
        if db is None:
            db = Database(config.database_path)
            await db.initialize()
            created_dbs.append(db)
        if git is None:
            git = create_autospec(GitManager, instance=True)
            git.slugify.side_effect = GitManager.slugify

        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = git

        bus = EventBus()
        services = build_internal_services(db=db, git=git, config=config)
        registry = PluginRegistry(db=db, bus=bus, config=config)
        registry._internal_services = services
        await registry.load_internal_plugins()
        orchestrator.plugin_registry = registry

        handler = CommandHandler(orchestrator, config)
        registry.set_active_project_id_getter(lambda: handler._active_project_id)
        handler._db = db
        handler._bus = bus
        handler._git = git
        handler._plugin_registry = registry
        return handler

    yield _make

    for db in created_dbs:
        await db.close()


# ---------------------------------------------------------------------------
# Review-pipeline test helpers — shared by test_review_pipeline_rules.py,
# test_review_pipeline_e2e.py, and test_review_reopen_cascade.py.
# ---------------------------------------------------------------------------

DEFAULT_PIPELINE_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "prompts"
    / "default_playbooks"
    / "default-pipeline.md"
)


@pytest.fixture
def command_handler_factory(tmp_path: Path):
    """Factory that creates a fresh CommandHandler backed by a real DB."""

    async def _make():
        from src.commands.handler import CommandHandler
        from src.config import AppConfig, DiscordConfig
        from src.database import Database
        from src.orchestrator import Orchestrator

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "test.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        h = CommandHandler(o, cfg)
        h._db = db  # stash for teardown / PipelineEngine hydration
        return h

    return _make


@pytest.fixture
async def session_orch(tmp_path: Path):
    """An orchestrator that dispatches the way production does: as a session.

    ``sessions.enabled`` + the ``fake`` provider + a ``claude`` harness, with
    git mocked out.  See ``tests/session_dispatch_helpers.py`` for the
    project/profile builders and the launch-evidence accessors that go with it.
    """
    from tests.session_dispatch_helpers import drain_running_tasks, make_session_orch

    orch = await make_session_orch(tmp_path)
    yield orch
    await drain_running_tasks(orch)
    await orch.shutdown()


@pytest.fixture
def orchestrator_factory(tmp_path: Path):
    """Factory that creates a fresh Orchestrator (with CommandHandler) backed
    by a real DB — for tests that exercise orchestrator-level sweeps."""

    async def _make():
        from src.commands.handler import CommandHandler
        from src.config import AppConfig, DiscordConfig
        from src.database import Database
        from src.orchestrator import Orchestrator

        db = Database(str(tmp_path / "orch.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "orch.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        o.command_handler = CommandHandler(o, cfg)
        return o

    return _make


class PipelineEngine:
    """Minimal test helper that loads a compiled pipeline and dispatches events.

    Dispatches the compiled rule subgraph that matches the given event type,
    injecting ``event.task`` from the DB when ``task_id`` is present (mirrors
    the orchestrator hydration path), and evaluates each rule's ``when``
    guard via the real ``_eval_pipeline_when`` (so ``all``/``any`` clauses
    behave identically to the runtime dispatch path).
    """

    def __init__(self, compiled, handler, db=None):
        self._compiled = compiled
        self._handler = handler
        self._db = db
        self._dispatched: set[tuple[str, str]] = set()  # (event_type, event_id)

    async def dispatch(
        self,
        event_type: str,
        payload: dict,
        *,
        event_id: str | None = None,
    ) -> None:
        from src.playbooks.pipeline_runner import PipelineRunner

        # Idempotency: same event_id dispatched twice is a no-op.
        key = (event_type, event_id) if event_id else None
        if key and key in self._dispatched:
            return
        if key:
            self._dispatched.add(key)

        # Hydrate event.task if task_id is present.
        hydrated = dict(payload)
        hydrated["_event_type"] = event_type
        if self._db and hydrated.get("task_id") and "task" not in hydrated:
            task_row = await self._db.get_task(str(hydrated["task_id"]))
            if task_row is not None:
                from dataclasses import asdict
                try:
                    hydrated["task"] = asdict(task_row)
                except Exception:
                    hydrated["task"] = (
                        vars(task_row) if hasattr(task_row, "__dict__") else {}
                    )

        # Select rule(s) for this event type.
        graph = self._compiled.to_dict()
        pipeline_rules = graph.get("pipeline_rules") or {}
        if not pipeline_rules:
            # Single-graph pipeline — run directly.
            runner = PipelineRunner(graph=graph, event=hydrated, handler=self._handler)
            await runner.run()
            return

        if event_type not in pipeline_rules:
            return  # No rule for this trigger

        rule_metas = pipeline_rules[event_type]
        # pipeline_rules[trigger] may be a single meta (legacy) or a list.
        if isinstance(rule_metas, (str, dict)):
            rule_metas = [rule_metas]

        import copy

        from src.orchestrator.core import _eval_pipeline_when

        for rule_meta in rule_metas:
            if isinstance(rule_meta, str):
                rule_entry = rule_meta
                rule_when = None
            else:
                rule_entry = rule_meta.get("entry", "")
                rule_when = rule_meta.get("when")

            # Evaluate ``when`` guard.
            if rule_when and not _eval_pipeline_when(rule_when, hydrated):
                continue

            # Clone graph, set the rule's entry node.
            run_graph = copy.deepcopy(graph)
            for nid, node in run_graph["nodes"].items():
                node["entry"] = nid == rule_entry

            runner = PipelineRunner(graph=run_graph, event=hydrated, handler=self._handler)
            await runner.run()


@pytest.fixture
def pipeline_engine_factory():
    """Factory that creates a PipelineEngine from the compiled default pipeline."""

    def _make(*, handler):
        from src.playbooks.pipeline_compiler import compile_pipeline

        md = DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8")
        result = compile_pipeline(md)
        assert result.success, f"default-pipeline.md did not compile: {result.errors}"
        db = getattr(handler, "_db", getattr(handler, "db", None))
        return PipelineEngine(result.playbook, handler, db=db)

    return _make
