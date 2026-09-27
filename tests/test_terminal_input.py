"""Input-only terminal against a real isolated tmux; a tiny raw-mode stub app, never an LLM."""
import asyncio
import contextlib
import os
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
    pytest.skip("isolated tmux input tests require POSIX tmux", allow_module_level=True)

from src.sessions.terminal_input import TmuxInputClient
from src.sessions.terminal_pty import TerminalAttachError
from src.sessions.tmux import TmuxProvider  # after the skip: POSIX only

# Raw mode: no echo, no ICRNL, no ISIG, so every byte tmux writes reaches the log.
# The modes are requested before READY, so once READY is on screen tmux has seen them.
STUB = """
import os, sys, tty
log, modes = sys.argv[1], sys.argv[2]
tty.setraw(0)
requests = b""
if "paste" in modes:
    requests += b"\\x1b[?2004h"
if "cursor" in modes:
    requests += b"\\x1b[?1h"
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
os.write(1, requests + b"READY\\r\\n")
while True:
    data = os.read(0, 4096)
    if not data:
        break
    os.write(fd, data)
"""


@pytest.fixture
async def pane(tmp_path, monkeypatch):
    """Start the stub app in session ``probe`` of a private tmux server."""
    # Never let the caller's tmux (an aq worker runs inside one) resolve targets.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    socket = "aq-input-test-" + uuid.uuid4().hex
    provider = TmuxProvider(SimpleNamespace(sessions=SimpleNamespace(tmux_socket=socket)))
    script = tmp_path / "stub.py"
    script.write_text(STUB)
    log = tmp_path / "received.bin"
    tmux_config = tmp_path / "tmux.conf"
    tmux_config.write_text("set -g status off\nset -g default-terminal tmux-256color\n")

    async def start(modes: str = "none"):
        command = "exec " + shlex.join([sys.executable, str(script), str(log), modes])
        await provider._tmux(
            "-f", str(tmux_config),
            "new-session", "-d", "-s", "probe", "-x", "200", "-y", "50", "-c", str(tmp_path),
            "-e", "AQ_INSTANCE_TOKEN=instance-a", command,
        )
        async with asyncio.timeout(5):
            while "READY" not in await provider._tmux("capture-pane", "-p", "-t", "=probe:"):
                await asyncio.sleep(0.02)
        row = SessionRecord(
            id="s", project_id=None, profile_id="test", harness="test", provider="tmux",
            name="probe", lifecycle="named", state="running", work_dir=str(tmp_path), epoch="e",
            instance_token="instance-a", started_at=time.time(),
        )
        return provider, row, log

    try:
        yield start
    finally:
        with contextlib.suppress(Exception):
            await provider._tmux("kill-server")


async def received(log, expected: bytes) -> bytes:
    async with asyncio.timeout(5):
        while True:
            data = log.read_bytes() if log.exists() else b""
            if len(data) >= len(expected):
                return data
            await asyncio.sleep(0.02)


async def geometry(provider) -> str:
    return (await provider._tmux(
        "display-message", "-p", "-t", "=probe:", "#{window_width}x#{window_height}",
    )).strip()


async def test_raw_bytes_utf8_ctrl_c_and_escape_reach_the_app_without_resizing(pane):
    provider, row, log = await pane()
    before = await geometry(provider)
    client = await TmuxInputClient.attach(provider, row)
    try:
        assert await client.verify()
        await client.write("héllo €".encode())
        await client.write(b"\x03")
        await client.write(b"\x1b")
        await client.write(b"\r")
        expected = "héllo €".encode() + b"\x03\x1b\r"
        assert await received(log, expected) == expected
    finally:
        await client.close()
    assert await geometry(provider) == before == "200x50"
    assert not (await provider._tmux("list-clients", "-F", "#{client_pid}")).strip()


@pytest.mark.parametrize("modes,expected", [
    ("paste", b"\x1b[200~line1\rline2\x1b[201~"),
    ("none", b"line1\rline2"),
])
async def test_paste_is_bracketed_only_when_the_app_asked_and_lf_becomes_cr(pane, modes, expected):
    provider, row, log = await pane(modes)
    client = await TmuxInputClient.attach(provider, row)
    try:
        await client.write(b"\x1b[200~line1\nline2\x1b[201~")
        assert await received(log, expected) == expected
        assert "aq-input-" not in await provider._tmux("list-buffers", "-F", "#{buffer_name}")
    finally:
        await client.close()


async def test_paste_split_across_writes_arrives_once(pane):
    provider, row, log = await pane("paste")
    client = await TmuxInputClient.attach(provider, row)
    try:
        await client.write(b"\x1b[200~first half ")
        await client.write(b"second half\x1b[20")
        await asyncio.sleep(0.1)
        assert not log.exists() or log.read_bytes() == b""  # nothing until the end marker
        await client.write(b"1~!")
        expected = b"\x1b[200~first half second half\x1b[201~!"
        assert await received(log, expected) == expected
        await asyncio.sleep(0.1)
        assert log.read_bytes() == expected
    finally:
        await client.close()


@pytest.mark.parametrize("modes,expected", [
    ("none", b"\x1b[A\x1b[D"),
    ("cursor", b"\x1bOA\x1bOD"),  # DECCKM: tmux encodes the key for the pane's mode
])
async def test_single_arrow_frames_follow_the_panes_cursor_key_mode(pane, modes, expected):
    provider, row, log = await pane(modes)
    client = await TmuxInputClient.attach(provider, row)
    try:
        await client.write(b"\x1b[A")
        await client.write(b"\x1bOD")
        assert await received(log, expected) == expected
    finally:
        await client.close()


async def test_copy_mode_is_left_so_keys_reach_the_app_not_the_hidden_mode(pane):
    provider, row, log = await pane()
    await provider._tmux("copy-mode", "-t", "=probe:")
    assert (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{pane_in_mode}")).strip() == "1"
    client = await TmuxInputClient.attach(provider, row)
    try:
        await client.write(b"after copy mode\r")
        assert await received(log, b"after copy mode\r") == b"after copy mode\r"
    finally:
        await client.close()
    assert (await provider._tmux("display-message", "-p", "-t", "=probe:", "#{pane_in_mode}")).strip() == "0"


async def test_changed_instance_token_refuses_writes(pane):
    provider, row, log = await pane()
    client = await TmuxInputClient.attach(provider, row)
    try:
        await provider._tmux("set-environment", "-t", "=probe", "AQ_INSTANCE_TOKEN", "successor")
        assert not await client.verify()
        with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
            await client.write(b"typed into a successor")
    finally:
        await client.close()
    await asyncio.sleep(0.1)
    assert not log.exists() or log.read_bytes() == b""
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await TmuxInputClient.attach(provider, row)


async def test_same_named_successor_with_same_token_refuses_writes(pane):
    provider, row, _ = await pane()
    client = await TmuxInputClient.attach(provider, row)
    try:
        await provider._tmux("rename-session", "-t", "=probe", "original")
        await provider._tmux(
            "new-session", "-d", "-s", "probe", "-e", "AQ_INSTANCE_TOKEN=instance-a", "cat",
        )
        assert not await client.verify()
        with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
            await client.write(b"x")
    finally:
        await client.close()


async def test_close_ends_reads_and_further_writes(pane):
    provider, row, _ = await pane()
    client = await TmuxInputClient.attach(provider, row)
    pending = asyncio.create_task(client.read(16384))
    await asyncio.sleep(0.05)
    assert not pending.done()  # an input-only terminal produces no output
    await client.close()
    assert await asyncio.wait_for(pending, 1) == b""
    assert not await client.verify()
    with pytest.raises(TerminalAttachError):
        await client.write(b"x")
    assert await provider._tmux("has-session", "-t", "=probe") == ""
