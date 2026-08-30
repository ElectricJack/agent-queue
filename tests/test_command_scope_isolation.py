"""``CommandHandler._current_scope`` is per-call identity, not shared state.

The scope dict ``/api/execute`` injects says *who is calling*: which
session, which task, which project, and whether the caller is elevated.
Every fence in the swarm surface reads it — ``_cmd_task_claim``,
``_assert_task_in_scope``, ``_cmd_create_task``'s worker-filing branch,
``_formula_scope_project``.

It used to be a plain instance attribute on a process-wide singleton,
written at the top of ``execute`` and cleared unconditionally in its
``finally``.  Both halves were wrong:

* **Nesting** — a command that dispatches another command lost its own
  identity as soon as the inner one returned.  ``aq task close
  --claim-next`` is exactly that shape.
* **Concurrency** — any command starting while another was awaiting
  blanked the first one's scope mid-flight.  ``task_close`` awaits the
  whole completion pipeline, so the window is seconds wide on a daemon
  serving more than one caller.

Both now hold because the value lives in a ContextVar that ``execute``
saves and restores.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig


class _Bus:
    async def emit(self, event_type, payload=None):
        return None


@pytest.fixture
def handler():
    orch = types.SimpleNamespace(
        db=None,
        bus=_Bus(),
        plugin_registry=None,
    )
    cfg = AppConfig()
    return CommandHandler(orch, cfg)


def _scope(session_id: str) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "task_id": None,
        "project_id": "p1",
        "elevated": False,
    }


class TestScopeIsolation:
    async def test_a_nested_command_does_not_strip_the_outer_scope(self, handler):
        """The ``task_close --claim-next`` shape, reduced to two stubs."""
        seen: dict[str, object] = {}

        async def _cmd_inner(args):
            seen["inner"] = handler._current_scope
            return {"success": True}

        async def _cmd_outer(args):
            seen["before"] = handler._current_scope
            await handler.execute("inner", {})
            seen["after"] = handler._current_scope
            return {"success": True}

        handler._cmd_inner = _cmd_inner
        handler._cmd_outer = _cmd_outer

        await handler.execute("outer", {"_scope": _scope("s-outer")})

        assert seen["before"]["session_id"] == "s-outer"
        # A re-entrant ``execute`` with no ``_scope`` of its own is an
        # unauthenticated internal call and gets no identity — unchanged.
        assert seen["inner"] is None
        # …but the outer command still has its own afterwards.  This is the
        # assertion that used to fail.
        assert seen["after"]["session_id"] == "s-outer"

    async def test_a_directly_dispatched_command_inherits_the_scope(self, handler):
        """``task_close --claim-next`` calls ``_cmd_task_claim`` in-process.

        No second ``execute``, so the identity simply has to still be there
        when the outer body reaches it — after however long the completion
        pipeline took.
        """
        seen: dict[str, object] = {}

        async def _cmd_claimish(args):
            seen["claim"] = handler._current_scope
            return {"success": True}

        async def _cmd_closeish(args):
            await asyncio.sleep(0)  # the pipeline's awaits, in miniature
            return await handler._cmd_claimish({})

        handler._cmd_claimish = _cmd_claimish
        handler._cmd_closeish = _cmd_closeish

        await handler.execute("closeish", {"_scope": _scope("s-worker")})
        assert seen["claim"]["session_id"] == "s-worker"

    async def test_concurrent_commands_keep_their_own_scope(self, handler):
        """Two agents mid-flight at once must not read each other's identity."""
        started = asyncio.Event()
        seen: dict[str, list] = {"a": [], "b": []}

        async def _cmd_slow(args):
            who = args["who"]
            seen[who].append(handler._current_scope)
            if who == "a":
                started.set()
                # Yield long enough for b to run start-to-finish inside a's
                # await — the shape of a completion pipeline awaiting git.
                await asyncio.sleep(0.05)
            else:
                await started.wait()
            seen[who].append(handler._current_scope)
            return {"success": True}

        handler._cmd_slow = _cmd_slow

        await asyncio.gather(
            handler.execute("slow", {"who": "a", "_scope": _scope("s-a")}),
            handler.execute("slow", {"who": "b", "_scope": _scope("s-b")}),
        )

        assert [s["session_id"] for s in seen["a"]] == ["s-a", "s-a"]
        assert [s["session_id"] for s in seen["b"]] == ["s-b", "s-b"]

    async def test_scope_does_not_leak_to_the_next_command(self, handler):
        """The original guarantee still holds: no bleed across requests."""
        seen: list = []

        async def _cmd_probe(args):
            seen.append(handler._current_scope)
            return {"success": True}

        handler._cmd_probe = _cmd_probe

        await handler.execute("probe", {"_scope": _scope("s1")})
        await handler.execute("probe", {})

        assert seen[0]["session_id"] == "s1"
        assert seen[1] is None

    async def test_client_supplied_scope_never_reaches_the_command_args(self, handler):
        """``_scope`` is popped before dispatch, as it always was."""
        seen: list = []

        async def _cmd_probe(args):
            seen.append(dict(args))
            return {"success": True}

        handler._cmd_probe = _cmd_probe
        await handler.execute("probe", {"x": 1, "_scope": _scope("s1")})
        assert seen[0] == {"x": 1}
