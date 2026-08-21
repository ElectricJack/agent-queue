"""Contract + registration tests for the interim Discord slash commands.

Covers the 1B adversarial-review findings:

* Fix 2 — ``/status`` called ``system_status``, which has never existed in
  ``CommandHandler``; every invocation returned ``Unknown command``.
* Fix 3 — five of six slash commands were registered (and synced to the
  guild) ahead of their backing commands, so they could only ever error.
* Fix 4 — ``src/discord/slash_commands.py`` had no tests at all.

The contract test below is the durable part: it fails the build when a
slash command's backing ``CommandHandler`` command name does not resolve
*and* is not on the explicit ``PENDING_BACKENDS`` allowlist.  A later wave
that ships ``gates_list`` instead of ``gate_list`` breaks here instead of
silently un-registering ``/gates``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.discord.slash_commands import (
    PENDING_BACKENDS,
    SLASH_COMMAND_BACKENDS,
    available_slash_commands,
    command_resolves,
    setup_commands,
)


# ---------------------------------------------------------------------------
# Fakes — no discord.py runtime objects, no database.
# ---------------------------------------------------------------------------


class FakeTree:
    """Stands in for ``discord.app_commands.CommandTree``.

    ``command()`` records the slash name and returns the undecorated
    coroutine so tests can invoke the command body directly.
    """

    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def command(self, *, name: str, description: str = ""):
        def decorator(func):
            self.commands[name] = func
            return func

        return decorator


class FakeHandler:
    """Minimal ``CommandHandler`` stand-in that records ``execute`` calls."""

    def __init__(self, *, commands: dict | None = None, plugin_registry=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results = commands or {}
        self.orchestrator = SimpleNamespace(plugin_registry=plugin_registry)
        for name in self._results:
            setattr(self, f"_cmd_{name}", self._make_cmd(name))

    def _make_cmd(self, name):
        async def _cmd(args):
            return self._results[name]

        return _cmd

    async def execute(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        if name in self._results:
            return self._results[name]
        return {"error": f"Unknown command: {name}"}


class FakeBot:
    def __init__(self, handler) -> None:
        self.tree = FakeTree()
        self.agent = SimpleNamespace(handler=handler)

    def get_project_for_channel(self, channel_id):
        return None


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = 0

    async def defer(self, *a, **kw):
        self.deferred += 1


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[tuple, dict]] = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1
        self.channel = SimpleNamespace(parent_id=None)


@pytest.fixture
def real_handler(tmp_path):
    """A real ``CommandHandler`` — no DB touched, only method lookup."""
    config = AppConfig(
        database_path=str(tmp_path / "t.db"),
        workspace_dir=str(tmp_path / "ws"),
        data_dir=str(tmp_path / "data"),
        messaging_platform="none",
    )
    orchestrator = SimpleNamespace(plugin_registry=None)
    return CommandHandler(orchestrator, config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The contract test (Fix 4)
# ---------------------------------------------------------------------------


class TestBackendContract:
    def test_every_backend_resolves_or_is_explicitly_pending(self, real_handler):
        """No silent name mismatches: resolve, or be on the allowlist."""
        unaccounted: list[str] = []
        for slash_name, backends in SLASH_COMMAND_BACKENDS.items():
            for backend in backends:
                if command_resolves(real_handler, backend):
                    continue
                if backend in PENDING_BACKENDS:
                    continue
                unaccounted.append(f"/{slash_name} -> {backend}")
        assert not unaccounted, (
            "Slash command backends neither implemented nor listed in "
            f"PENDING_BACKENDS: {unaccounted}. Either the owning wave renamed "
            "the command (fix SLASH_COMMAND_BACKENDS) or it has not landed "
            "yet (add it to PENDING_BACKENDS)."
        )

    def test_pending_allowlist_shrinks(self, real_handler):
        """A landed backend must be removed from PENDING_BACKENDS."""
        landed = [b for b in PENDING_BACKENDS if command_resolves(real_handler, b)]
        assert not landed, (
            f"These backends now exist and must be dropped from PENDING_BACKENDS: {sorted(landed)}"
        )

    def test_pending_allowlist_only_names_real_backends(self):
        """No stale entries: every pending name is actually referenced."""
        referenced = {b for backends in SLASH_COMMAND_BACKENDS.values() for b in backends}
        assert PENDING_BACKENDS <= referenced

    def test_status_no_longer_targets_system_status(self, real_handler):
        """Fix 2: ``system_status`` does not exist; ``get_status`` does."""
        assert not command_resolves(real_handler, "system_status")
        assert command_resolves(real_handler, "get_status")
        assert SLASH_COMMAND_BACKENDS["status"] == ("get_status", "list_tasks")

    def test_implemented_backends_really_resolve(self, real_handler):
        assert command_resolves(real_handler, "get_status")
        assert command_resolves(real_handler, "list_tasks")


# ---------------------------------------------------------------------------
# command_resolves()
# ---------------------------------------------------------------------------


class TestCommandResolves:
    def test_unknown_command_does_not_resolve(self, real_handler):
        assert command_resolves(real_handler, "definitely_not_a_command") is False

    def test_none_handler_does_not_resolve(self):
        assert command_resolves(None, "list_tasks") is False

    def test_plugin_registered_bare_name_resolves(self):
        """``PluginContext.register_command`` registers a bare name too."""

        async def _plugin_cmd(args):
            return {}

        registry = SimpleNamespace(
            get_command=lambda n: _plugin_cmd if n in ("gate_list", "myplug.gate_list") else None
        )
        handler = FakeHandler(plugin_registry=registry)
        assert command_resolves(handler, "gate_list") is True
        assert command_resolves(handler, "myplug.gate_list") is True
        assert command_resolves(handler, "other") is False

    def test_registry_failure_is_not_fatal(self):
        def _boom(name):
            raise RuntimeError("registry half-initialised")

        handler = FakeHandler(plugin_registry=SimpleNamespace(get_command=_boom))
        assert command_resolves(handler, "gate_list") is False


# ---------------------------------------------------------------------------
# Conditional registration (Fix 3)
# ---------------------------------------------------------------------------


class TestConditionalRegistration:
    def test_only_backed_commands_are_registered(self, real_handler):
        bot = FakeBot(real_handler)
        registered = setup_commands(bot)
        # Every command's backend has now landed: /peek and /attach with the
        # sessions lane, /explain and /gates with the work-graph lane --
        # which is the mechanism working, not a regression (see the test
        # below).
        expected = ["attach", "explain", "gates", "peek", "status", "tasks"]
        assert registered == expected
        assert set(bot.tree.commands) == set(expected)

    def test_command_appears_once_its_backend_lands(self, real_handler):
        """No change needed here when a later wave ships ``gate_list``."""

        async def _cmd_gate_list(args):
            return {"gates": []}

        real_handler._cmd_gate_list = _cmd_gate_list  # type: ignore[attr-defined]
        bot = FakeBot(real_handler)
        registered = setup_commands(bot)
        assert "gates" in registered
        assert "gates" in bot.tree.commands

    def test_plugin_provided_backend_enables_registration(self, real_handler):
        async def _plugin_cmd(args):
            return {}

        real_handler.orchestrator.plugin_registry = SimpleNamespace(  # type: ignore[attr-defined]
            get_command=lambda n: _plugin_cmd if n == "session_peek" else None
        )
        bot = FakeBot(real_handler)
        registered = setup_commands(bot)
        assert "peek" in registered

    def test_partial_backend_blocks_registration(self):
        """``/status`` needs both backends; one is not enough."""
        handler = FakeHandler(commands={"get_status": {}})
        bot = FakeBot(handler)
        registered = setup_commands(bot)
        assert "status" not in registered
        assert available_slash_commands(handler)["status"] == ["list_tasks"]

    def test_no_command_registered_when_handler_is_bare(self):
        bot = FakeBot(FakeHandler())
        assert setup_commands(bot) == []
        assert bot.tree.commands == {}


# ---------------------------------------------------------------------------
# Command bodies
# ---------------------------------------------------------------------------


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_status_calls_get_status_and_list_tasks(self):
        handler = FakeHandler(
            commands={
                "get_status": {
                    "projects": 2,
                    "orchestrator_paused": False,
                    "tasks": {"total": 3, "by_status": {"READY": 1, "COMPLETED": 2}},
                },
                "list_tasks": {"tasks": [{"id": "t1", "status": "READY", "title": "Do the thing"}]},
            }
        )
        bot = FakeBot(handler)
        setup_commands(bot)
        interaction = FakeInteraction()

        await bot.tree.commands["status"](interaction)

        assert [name for name, _ in handler.calls] == ["get_status", "list_tasks"]
        # Fix 2 regression guard.
        assert "system_status" not in [name for name, _ in handler.calls]
        # The interaction may only be deferred once.
        assert interaction.response.deferred == 1
        content = interaction.followup.sent[-1][0][0]
        assert "System Status" in content
        assert "t1" in content
        assert "Do the thing" in content

    @pytest.mark.asyncio
    async def test_status_shows_paused_orchestrator(self):
        handler = FakeHandler(
            commands={
                "get_status": {"projects": 1, "orchestrator_paused": True, "tasks": {"total": 0}},
                "list_tasks": {"tasks": []},
            }
        )
        bot = FakeBot(handler)
        setup_commands(bot)
        interaction = FakeInteraction()
        await bot.tree.commands["status"](interaction)
        content = interaction.followup.sent[-1][0][0]
        assert "PAUSED" in content
        assert "No active tasks" in content

    @pytest.mark.asyncio
    async def test_status_reports_backend_error_once(self):
        handler = FakeHandler(commands={"get_status": {"error": "boom"}, "list_tasks": {}})
        bot = FakeBot(handler)
        setup_commands(bot)
        interaction = FakeInteraction()
        await bot.tree.commands["status"](interaction)
        # get_status failed → list_tasks is never called, one error reply sent.
        assert [name for name, _ in handler.calls] == ["get_status"]
        assert len(interaction.followup.sent) == 1
        assert "embed" in interaction.followup.sent[0][1]


class TestTasksCommand:
    @pytest.mark.asyncio
    async def test_tasks_lists_tasks(self):
        handler = FakeHandler(
            commands={
                "list_tasks": {
                    "tasks": [
                        {"id": "t1", "status": "READY", "title": "Alpha"},
                        {"id": "t2", "status": "DONE", "title": "Beta"},
                    ]
                }
            }
        )
        bot = FakeBot(handler)
        setup_commands(bot)
        interaction = FakeInteraction()
        await bot.tree.commands["tasks"](interaction)
        name, args = handler.calls[0]
        assert name == "list_tasks"
        assert args["display_mode"] == "flat"
        content = interaction.followup.sent[-1][0][0]
        assert "Alpha" in content and "Beta" in content

    @pytest.mark.asyncio
    async def test_tasks_empty_sends_info_embed(self):
        handler = FakeHandler(commands={"list_tasks": {"tasks": []}})
        bot = FakeBot(handler)
        setup_commands(bot)
        interaction = FakeInteraction()
        await bot.tree.commands["tasks"](interaction)
        assert "embed" in interaction.followup.sent[-1][1]
