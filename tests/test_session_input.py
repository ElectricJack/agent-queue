"""Direct terminal input must target one live instance without message delivery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Agent, AgentProfile, SessionRecord
from src.sessions.provider import SessionError, SessionHandle
from src.sessions.tmux import TmuxProvider


@pytest.fixture
async def terminal(tmp_path):
    db = Database(str(tmp_path / "terminal.db"))
    await db.initialize()
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_agent(Agent(id="agent-a", name="Ada", profile_id="worker"))
    row = SessionRecord(
        id="session-a", name="n-agent-a", agent_id="agent-a", project_id=None,
        profile_id="worker", harness="claude", provider="tmux", lifecycle="named",
        work_dir=str(tmp_path), epoch="epoch", instance_token="instance-a",
        started_at=1, state="running",
    )
    await db.create_session(row)
    provider = SimpleNamespace(name="tmux", supports=lambda cap: True, send_input=AsyncMock())
    config = AppConfig()
    orchestrator = SimpleNamespace(
        db=db, bus=EventBus(validate_events=False),
        session_providers=SimpleNamespace(create=lambda *args: provider),
    )
    handler = CommandHandler(orchestrator, config)
    yield handler, provider, row
    handler._current_scope = None
    await db.close()


async def test_direct_input_preserves_literal_text_and_does_not_nudge(terminal):
    handler, provider, row = terminal
    result = await handler.execute("session_input", {
        "session_id": row.id, "text": "  café 🍵  ",
    })
    assert result == {"success": True, "session_id": row.id, "accepted": True}
    provider.send_input.assert_awaited_once_with(
        SessionHandle(row.name, row.provider, row.instance_token),
        text="  café 🍵  ", key=None,
    )


async def test_direct_key_is_sent_without_a_chat_envelope(terminal):
    handler, provider, row = terminal
    result = await handler.execute("session_input", {"session_id": row.id, "key": "Enter"})
    assert result.get("accepted") is True
    provider.send_input.assert_awaited_once_with(
        SessionHandle(row.name, row.provider, row.instance_token), text=None, key="Enter",
    )


@pytest.mark.parametrize("payload", [
    {}, {"text": ""}, {"text": 3}, {"text": "x", "key": "Enter"},
    {"key": "kill-session"}, {"key": ["Enter"]}, {"text": "\x00"},
    {"text": "é" * 32769},
])
async def test_invalid_input_never_reaches_provider(terminal, payload):
    handler, provider, row = terminal
    result = await handler.execute("session_input", {"session_id": row.id, **payload})
    assert "error" in result
    provider.send_input.assert_not_awaited()


@pytest.mark.parametrize("state", ["stopped", "sleeping", "quarantined"])
async def test_inactive_terminal_is_not_woken_by_input(terminal, state):
    handler, provider, row = terminal
    await handler.db.update_session(row.id, state=state)
    result = await handler.execute("session_input", {"session_id": row.id, "text": "hello"})
    assert "error" in result and "live" in result["error"].lower()
    provider.send_input.assert_not_awaited()


async def test_unsupported_provider_does_not_silently_accept_input(terminal):
    handler, provider, row = terminal
    provider.supports = lambda cap: False
    result = await handler.execute("session_input", {"session_id": row.id, "text": "hello"})
    assert "error" in result
    provider.send_input.assert_not_awaited()


async def test_input_errors_and_debug_logs_never_echo_typed_content(terminal, caplog):
    handler, provider, row = terminal
    secret = "private-terminal-text-830"
    provider.send_input.side_effect = SessionError("provider failed with " + secret)
    with caplog.at_level("DEBUG", logger="src.commands.handler"):
        result = await handler.execute("session_input", {"session_id": row.id, "text": secret})
    assert "error" in result
    assert secret not in str(result)
    assert secret not in caplog.text


@pytest.mark.parametrize("scope", [
    RequestScope(kind="session", session_id="own", project_id="p", elevated=True),
    RequestScope(kind="session", session_id="own", project_id=None, elevated=False),
])
def test_scoped_agent_cannot_type_into_another_terminal(scope):
    assert check_command_scope("session_input", {"session_id": "other"}, scope)


async def test_typed_input_endpoint_checks_scope_and_sends_literal_text(terminal):
    from fastapi import FastAPI
    from src.api.auth import LOCAL_SCOPE
    from src.api.codegen import build_category_routers
    from src.api.dependencies import get_command_handler

    handler, provider, row = terminal
    app = FastAPI()
    for router in build_category_routers():
        if router.prefix == "/api/system":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler
    scope = LOCAL_SCOPE

    @app.middleware("http")
    async def set_scope(request, call_next):
        request.state.scope = scope
        return await call_next(request)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/system/session-input", json={"session_id": row.id, "text": "hi"})
        assert response.status_code == 200
        assert response.json()["accepted"] is True
        scope = RequestScope(kind="session", project_id="p", session_id="other", elevated=True)
        denied = await client.post("/api/system/session-input", json={"session_id": row.id, "key": "Enter"})
        assert denied.status_code == 403
    assert provider.send_input.await_count == 1


@pytest.fixture
def tmux_input():
    provider = TmuxProvider()
    calls = []

    async def tmux(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "show-environment":
            return "AQ_INSTANCE_TOKEN=instance-a\n"
        if args[0] == "display-message":
            return "0"
        return ""

    provider._tmux = tmux
    provider._find_agent_pane = AsyncMock(return_value="%42")
    provider._process_names_hint = AsyncMock(return_value=("claude",))
    return provider, calls


async def test_tmux_text_is_literal_and_never_auto_submits(tmux_input):
    provider, calls = tmux_input
    text = "Enter; $(do-not-run) café"
    await provider.send_input(SessionHandle("n-agent-a", "tmux", "instance-a"), text=text)
    sends = [args for args, _ in calls if args[0] == "send-keys" and "-X" not in args]
    assert sends == [("send-keys", "-t", "%42", "-l", "--", text)]


async def test_tmux_checks_live_instance_even_when_token_cache_is_stale(tmux_input):
    provider, calls = tmux_input
    provider._token_cache["n-agent-a"] = "stale-instance"
    with pytest.raises(SessionError):
        await provider.send_input(SessionHandle("n-agent-a", "tmux", "stale-instance"), key="Enter")
    assert not any(args[0] == "send-keys" for args, _ in calls)


async def test_tmux_special_keys_are_allowlisted(tmux_input):
    provider, calls = tmux_input
    with pytest.raises(ValueError):
        await provider.send_input(SessionHandle("n-agent-a", "tmux", "instance-a"), key="kill-session")
    assert not calls


async def test_tmux_long_paste_is_buffered_without_pressing_enter(tmux_input):
    provider, calls = tmux_input
    payload = "hello\n" * 1000
    await provider.send_input(SessionHandle("n-agent-a", "tmux", "instance-a"), text=payload)
    loaded = [(args, kwargs) for args, kwargs in calls if args[0] == "load-buffer"]
    assert len(loaded) == 1 and loaded[0][1]["stdin"] == payload.encode()
    pasted = [args for args, _ in calls if args[0] == "paste-buffer"]
    assert len(pasted) == 1 and "-p" in pasted[0] and "%42" in pasted[0]
    assert not any(args[0] == "send-keys" and "Enter" in args for args, _ in calls)


async def test_direct_input_round_trip_through_a_real_isolated_tmux(tmp_path):
    import asyncio
    import contextlib
    import shlex
    import shutil
    import sys
    import uuid

    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    socket = "aq-input-test-" + uuid.uuid4().hex
    provider = TmuxProvider(SimpleNamespace(
        data_dir=str(tmp_path), sessions=SimpleNamespace(tmux_socket=socket),
    ))
    script = tmp_path / "echo-input.py"
    script.write_text(
        "import sys\n"
        "print('TERMINAL_READY', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('RECEIVED:' + line.rstrip('\\n'), flush=True)\n"
    )
    name = "n-input-test"
    handle = SessionHandle(name, "tmux", "test-instance")

    async def wait_for(marker):
        for _ in range(100):
            screen = await provider._tmux("capture-pane", "-p", "-t", "=" + name + ":")
            if marker in screen:
                return screen
            await asyncio.sleep(0.05)
        pytest.fail("Expected terminal output was not received")

    try:
        await provider._tmux(
            "new-session", "-d", "-s", name, "-x", "100", "-y", "24",
            shlex.join([sys.executable, "-u", str(script)]),
        )
        await provider._tmux("set-environment", "-t", "=" + name, "AQ_INSTANCE_TOKEN", handle.instance_token)
        await wait_for("TERMINAL_READY")
        await provider.send_input(handle, text="hello")
        await provider.send_input(handle, key="BSpace")
        await provider.send_input(handle, text="o café")
        before_enter = await provider._tmux("capture-pane", "-p", "-t", "=" + name + ":")
        assert "RECEIVED:hello café" not in before_enter
        await provider.send_input(handle, key="Enter")
        await wait_for("RECEIVED:hello café")
    finally:
        # This socket is created only by this test, never the live AQ server.
        assert socket.startswith("aq-input-test-")
        with contextlib.suppress(Exception):
            await provider._tmux("kill-server")


@pytest.mark.parametrize("command,args", [
    ("create_task", {"project_id": "other", "title": "unauthorized"}),
    ("task_set", {"project_id": "other", "task_id": "other-task"}),
    ("task_close", {"task_id": "other-task"}),
    ("task_claim", {"project_id": "other"}),
    ("message_send", {"to_kind": "session", "to_id": "other-session", "body": "hi"}),
    ("memory_save", {"project_id": "other", "content": "private"}),
])
def test_unassigned_interactive_token_does_not_mean_global_scope(command, args):
    scope = RequestScope(kind="session", session_id="interactive", project_id=None, elevated=False)
    error = check_command_scope(command, args, scope)
    assert error and "scope" in error


def test_unassigned_interactive_token_can_read_its_own_bootstrap_schema():
    scope = RequestScope(kind="session", session_id="interactive", project_id=None, elevated=False)
    assert check_command_scope("prime", {}, scope) is None
    assert check_command_scope("get_schema", {}, scope) is None
    assert check_command_scope("create_task", {"project_id": "p"}, RequestScope(
        kind="session", session_id="supervisor", project_id=None, elevated=True,
    )) is None
