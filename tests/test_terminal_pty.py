"""Real isolated tmux + PTY; tiny echo process only, never an LLM or live socket."""
import asyncio
import importlib.util
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from src.models import SessionRecord

pytestmark = pytest.mark.tmux
if os.name != "posix" or shutil.which("tmux") is None:
    pytest.skip("isolated tmux PTY tests require POSIX tmux", allow_module_level=True)

from src.sessions.tmux import TmuxProvider  # noqa: E402 - skip non-POSIX hosts first

STUB = """
import os, tty
import signal
fd=0
tty.setraw(fd)
print('\\x1b[38;2;12;34;56mCOLOR\\x1b[0m', flush=True)
def winch(*args):
    print('RESIZED', flush=True)
signal.signal(signal.SIGWINCH, winch)
while True:
    data=os.read(fd,4096)
    if not data: break
    os.write(1,b'ECHO:'+data)
""".replace("\\x1b", "\x1b")


@pytest.fixture
async def running(tmp_path):
    socket = "aq-pty-test-" + uuid.uuid4().hex
    config = SimpleNamespace(sessions=SimpleNamespace(tmux_socket=socket))
    provider = TmuxProvider(config)
    tmux_config = tmp_path / "tmux.conf"
    tmux_config.write_text("set -g status off\nset -g default-terminal tmux-256color\n")
    script = tmp_path / "echo.py"
    script.write_text(STUB)
    command = "exec " + shlex.join([sys.executable, str(script)])
    await provider._tmux(
        "-f", str(tmux_config),
        "new-session", "-d", "-s", "probe", "-c", str(tmp_path),
        "-e", "AQ_INSTANCE_TOKEN=instance-a", command,
    )
    row = SessionRecord(
        id="s", project_id=None, profile_id="test", harness="test", provider="tmux",
        name="probe", lifecycle="named", state="running", work_dir=str(tmp_path), epoch="e",
        instance_token="instance-a", started_at=time.time(),
    )
    try:
        yield provider, row
    finally:
        await provider._tmux("kill-server")


def implementation():
    assert importlib.util.find_spec("src.sessions.terminal_pty"), "PTY attach client is required"
    from src.sessions.terminal_pty import PtyTmuxClient
    return PtyTmuxClient


async def read_until(client, marker):
    data = bytearray()
    async with asyncio.timeout(5):
        while marker not in data:
            chunk = await client.read(16384)
            assert chunk, "attach client exited before output arrived"
            data.extend(chunk)
    return bytes(data)


async def test_real_pty_preserves_truecolor_input_resize_and_agent_on_detach(running):
    provider, row = running
    before = (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{pane_pid}")).strip()
    client = await implementation().attach(provider, row, cols=100, rows=30)
    try:
        assert await client.verify()
        initial = await read_until(client, b"COLOR")
        assert b"38;2;12;34;56" in initial
        await client.write(b"hello")
        assert b"ECHO:hello" in await read_until(client, b"ECHO:hello")
        await client.resize(123, 41)
        await read_until(client, b"RESIZED")
        geometry = (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{window_width},#{window_height}")).strip()
        assert geometry == "123,41"
    finally:
        await client.close()
    after = (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{pane_pid}")).strip()
    assert after == before
    assert not (await provider._tmux("list-clients", "-F", "#{client_pid}")).strip()


async def test_pty_generation_fence_rejects_changed_instance(running):
    provider, row = running
    client = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        await provider._tmux("set-environment", "-t", "=probe", "AQ_INSTANCE_TOKEN", "successor")
        assert not await client.verify()
    finally:
        await client.close()
    from src.sessions.terminal_pty import TerminalAttachError
    with pytest.raises(TerminalAttachError):
        await implementation().attach(provider, row, cols=80, rows=24)
    assert not (await provider._tmux("list-clients", "-F", "#{client_pid}")).strip()


async def test_attachment_rechecks_generation_before_returning(running, monkeypatch):
    provider, row = running
    original = provider._tmux
    changed = False

    async def change_after_attach(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if args[0] == "list-clients" and result.strip() and not changed:
            changed = True
            await original("set-environment", "-t", "=probe", "AQ_INSTANCE_TOKEN", "successor")
        return result

    monkeypatch.setattr(provider, "_tmux", change_after_attach)
    from src.sessions.terminal_pty import TerminalAttachError
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await implementation().attach(provider, row, cols=80, rows=24)
    assert changed
    assert not (await original("list-clients", "-F", "#{client_pid}")).strip()
    assert await original("has-session", "-t", "=probe") == ""


async def test_verify_rejects_reused_name_even_with_same_token(running):
    provider, row = running
    client = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        await provider._tmux("rename-session", "-t", "=probe", "original")
        command = "exec " + shlex.join([sys.executable, os.path.join(row.work_dir, "echo.py")])
        await provider._tmux(
            "new-session", "-d", "-s", "probe", "-e", "AQ_INSTANCE_TOKEN=instance-a", command,
        )
        assert not await client.verify()
    finally:
        await client.close()
    assert await provider._tmux("has-session", "-t", "=original") == ""
    assert await provider._tmux("has-session", "-t", "=probe") == ""


async def test_cancelled_attach_reaps_only_its_client(running, monkeypatch):
    provider, row = running
    original = provider._tmux
    attached = asyncio.Event()
    wait_forever = asyncio.Event()

    async def block_after_attach(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[0] == "list-clients" and result.strip():
            attached.set()
            await wait_forever.wait()
        return result

    monkeypatch.setattr(provider, "_tmux", block_after_attach)
    task = asyncio.create_task(implementation().attach(provider, row, cols=80, rows=24))
    try:
        await asyncio.wait_for(attached.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not (await original("list-clients", "-F", "#{client_pid}")).strip()
    assert await original("has-session", "-t", "=probe") == ""


async def test_close_wakes_pending_reader_and_reattach_still_works(running):
    provider, row = running
    client = await implementation().attach(provider, row, cols=80, rows=24)
    pending = None
    try:
        await read_until(client, b"COLOR")
        # Drain any initial tmux redraw. Cancelling a blocked read must unregister
        # its FD watcher, and closing must wake a subsequent read with EOF.
        while True:
            try:
                await asyncio.wait_for(client.read(16384), 0.05)
            except asyncio.TimeoutError:
                break
        pending = asyncio.create_task(client.read(16384))
        await asyncio.sleep(0.025)
        assert not pending.done()
        await client.close()
        assert await asyncio.wait_for(pending, 1) == b""
        assert await client.read(16384) == b""
    finally:
        await client.close()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    second = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        await read_until(second, b"COLOR")
        await second.write(b"fresh")
        await read_until(second, b"ECHO:fresh")
    finally:
        await second.close()


async def test_attach_errors_do_not_expose_provider_diagnostics(running, monkeypatch):
    provider, row = running

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive command/environment/terminal content")

    monkeypatch.setattr(provider, "_tmux", broken)
    from src.sessions.terminal_pty import TerminalAttachError
    with pytest.raises(TerminalAttachError) as caught:
        await implementation().attach(provider, row, cols=80, rows=24)
    assert str(caught.value) == "Terminal session is unavailable."
    monkeypatch.undo()


async def test_verify_rejects_client_switched_to_another_session(running):
    provider, row = running
    client = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        command = "exec " + shlex.join([sys.executable, os.path.join(row.work_dir, "echo.py")])
        await provider._tmux("new-session", "-d", "-s", "other", command)
        tty = (await provider._tmux("list-clients", "-F", "#{client_tty}")).strip()
        await provider._tmux("switch-client", "-c", tty, "-t", "=other")
        assert not await client.verify()
    finally:
        await client.close()
    assert await provider._tmux("has-session", "-t", "=probe") == ""
    assert await provider._tmux("has-session", "-t", "=other") == ""


async def test_attachment_does_not_apply_client_environment_to_agent(running, monkeypatch):
    provider, row = running
    session_id = (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{session_id}")).strip()
    await provider._tmux("set-option", "-t", session_id, "update-environment", "AQ_INSTANCE_TOKEN")
    monkeypatch.delenv("AQ_INSTANCE_TOKEN", raising=False)
    client = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        assert await client.verify()
        value = await provider._tmux("show-environment", "-t", "=probe", "AQ_INSTANCE_TOKEN")
        assert value.strip() == "AQ_INSTANCE_TOKEN=instance-a"
    finally:
        await client.close()


@pytest.mark.parametrize("policy", ["off", "no-detached", "previous", "next"])
async def test_attachment_rejects_automatic_session_switching(running, policy):
    provider, row = running
    session_id = (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{session_id}")).strip()
    await provider._tmux("set-option", "-t", session_id, "detach-on-destroy", policy)
    from src.sessions.terminal_pty import TerminalAttachError
    client = None
    try:
        with pytest.raises(TerminalAttachError, match="detach-on-destroy on"):
            client = await implementation().attach(provider, row, cols=80, rows=24)
    finally:
        if client is not None:
            await client.close()
    assert not (await provider._tmux("list-clients", "-F", "#{client_pid}")).strip()
    assert await provider._tmux("has-session", "-t", "=probe") == ""


async def test_verify_rechecks_effective_detach_policy(running):
    provider, row = running
    client = await implementation().attach(provider, row, cols=80, rows=24)
    try:
        # Inherited global changes must be observed, not just session overrides.
        await provider._tmux("set-option", "-g", "detach-on-destroy", "off")
        assert not await client.verify()
    finally:
        await client.close()


async def _window(provider) -> str:
    return (await provider._tmux(
        "display-message", "-p", "-t", "=probe:", "#{window_width}x#{window_height}",
    )).strip()


async def _wait_window(provider, size):
    async with asyncio.timeout(5):
        while await _window(provider) != size:
            await asyncio.sleep(0.02)


async def test_phone_attach_restores_the_resting_window_size_after_the_last_client(running):
    provider, row = running
    await provider._tmux("set-option", "-w", "-t", "=probe:", "window-size", "latest")
    await provider._tmux("resize-window", "-t", "=probe:", "-x", "132", "-y", "43")
    await provider._tmux("set-option", "-w", "-t", "=probe:", "window-size", "latest")
    first = await implementation().attach(provider, row, cols=45, rows=20, restore_size=True)
    await _wait_window(provider, "45x20")
    second = await implementation().attach(provider, row, cols=50, rows=22, restore_size=True)
    await _wait_window(provider, "50x22")
    await first.close()
    # Another phone is still watching: the window stays sized for it.
    await asyncio.sleep(0.1)
    assert await _window(provider) == "50x22"
    await second.close()
    assert await _window(provider) == "132x43"
    assert (await provider._tmux(
        "show-options", "-wv", "-t", "=probe:", "window-size",
    )).strip() == "latest"
    assert not (await provider._tmux(
        "show-options", "-qv", "-t", "=probe", "@aq_restore_size",
    )).strip()
    assert not (await provider._tmux("list-clients", "-F", "#{client_pid}")).strip()


async def test_desktop_attach_keeps_its_size_and_a_phone_leaving_it_restores_nothing(running):
    provider, row = running
    desktop = await implementation().attach(provider, row, cols=120, rows=40)
    await _wait_window(provider, "120x40")
    phone = await implementation().attach(provider, row, cols=45, rows=20, restore_size=True)
    await _wait_window(provider, "45x20")
    await phone.close()
    await _wait_window(provider, "120x40")
    await desktop.close()
    assert await _window(provider) == "120x40"


async def test_a_phone_leaving_a_desktop_behind_leaves_no_stale_record(running):
    provider, row = running
    desktop = await implementation().attach(provider, row, cols=120, rows=40)
    await _wait_window(provider, "120x40")
    phone = await implementation().attach(provider, row, cols=45, rows=20, restore_size=True)
    await _wait_window(provider, "45x20")
    await phone.close()
    await _wait_window(provider, "120x40")
    assert not (await provider._tmux(
        "show-options", "-qv", "-t", "=probe", "@aq_restore_size",
    )).strip()
    await desktop.close()
    # Later a different desktop and a phone; the desktop leaves first, so the
    # phone restores the size from when it joined, not the first desktop's.
    desktop = await implementation().attach(provider, row, cols=100, rows=30)
    await _wait_window(provider, "100x30")
    phone = await implementation().attach(provider, row, cols=45, rows=20, restore_size=True)
    await _wait_window(provider, "45x20")
    await desktop.close()
    await phone.close()
    assert await _window(provider) == "100x30"


async def test_history_frames_scrollback_for_a_fresh_terminal(tmp_path, running):
    provider, row = running
    script = tmp_path / "lines.py"
    script.write_text(
        "import time\n"
        "for i in range(60):\n"
        "    print(f'\\x1b[32mline {i:02d}\\x1b[0m' + ' ' * 20 + '\\x1b[0m', flush=True)\n"
        "time.sleep(60)\n"
    )
    await provider._tmux("kill-session", "-t", "=probe")
    await provider._tmux(
        "new-session", "-d", "-s", "probe", "-x", "80", "-y", "10",
        "-e", "AQ_INSTANCE_TOKEN=instance-a",
        "exec " + shlex.join([sys.executable, str(script)]),
    )
    async with asyncio.timeout(5):
        while "line 59" not in await provider._tmux("capture-pane", "-p", "-t", "=probe:"):
            await asyncio.sleep(0.05)
    client = await implementation().attach(provider, row, cols=80, rows=10)
    try:
        assert await client.history(0, 10) == b""
        framed = (await client.history(20, 6)).decode()
        text = (await client.history(1000, 6)).decode()
    finally:
        await client.close()
    assert framed.endswith("\x1b[0m" + "\r\n" * 6)
    lines = framed[: -len("\r\n" * 6)].split("\x1b[0m\r\n")
    assert len(lines) == 20
    # Only scrollback: the visible screen (the last lines) arrives with the attach.
    assert "line 59" not in framed
    assert all(not line.endswith(" ") for line in lines)
    assert "\x1b[32m" in lines[-1]
    body = text[: -len("\r\n" * 6)]
    assert "line 00" in body and body.count("line ") == len(body.split("\x1b[0m\r\n"))


async def _burst(client):
    # One write of many lines: tmux flushes the scroll as a single multi-line step.
    await client.write(b"x\r\n" * 100 + b"END")
    return await read_until(client, b"END")


async def test_scrollback_attach_makes_tmux_scroll_with_line_feeds(running):
    provider, row = running
    attach = implementation().attach
    desktop = await attach(provider, row, cols=80, rows=24)
    try:
        await read_until(desktop, b"COLOR")
        assert re.search(rb"\x1b\[[0-9]+S", await _burst(desktop))
    finally:
        await desktop.close()
    assert not (await provider._tmux("show-options", "-sv", "terminal-overrides")).strip()
    for _ in range(2):
        phone = await attach(provider, row, cols=80, rows=24, scrollback=True)
        try:
            await read_until(phone, b"END")
            assert not re.search(rb"\x1b\[[0-9]+S", await _burst(phone))
        finally:
            await phone.close()
    overrides = await provider._tmux("show-options", "-sv", "terminal-overrides")
    assert overrides.splitlines() == ["xterm-256color:indn@"]
