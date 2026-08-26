"""TmuxProvider against a real tmux server (Linux CI only).

Everything runs on an isolated per-test socket so a developer's own tmux
server (and the daemon's ``aq`` socket) is never touched.  The stub agent
is a tiny raw-mode REPL that behaves like a harness TUI: optional startup
dialog, a ``❯`` prompt with a trailing NBSP (exactly what Claude paints),
and every submitted line appended to ``received.txt`` so delivery is
asserted on disk, not by scraping the screen.

Spec: docs/specs/implementation/session-runtime.md §8 (test_tmux_integration
row) and the §9 pitfall table.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

if os.name != "posix":  # pragma: no cover
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions import proctable
from src.sessions.provider import DialogRule, SessionSpec
from src.sessions.tmux import TmuxProvider

pytestmark = pytest.mark.tmux

NBSP = " "
PROMPT = f"❯{NBSP}"  # "❯ " with a non-breaking space, as Claude paints it

#: Raw-mode REPL stub.  ``--dialog`` shows a trust dialog first and waits
#: for any key.  Reads bytes (not lines — canonical mode truncates at 4 KB,
#: which is exactly why real TUIs run raw), strips bracketed-paste markers,
#: appends each submitted line to received.txt, and repaints the prompt.
STUB = r"""
import os, sys, termios

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ICANON | termios.ECHO)
termios.tcsetattr(fd, termios.TCSANOW, attrs)

if "--dialog" in sys.argv:
    print("Do you trust the files in this folder?", flush=True)
    os.read(fd, 64)  # any key dismisses

print("❯ ", flush=True)
buf = b""
while True:
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    if "--mute" not in sys.argv:
        # Render typed input like a real TUI input line (termios ECHO is
        # off).  --mute simulates a harness mid-turn that swallows keys.
        sys.stdout.write(chunk.decode("utf-8", "replace").replace("\r", "\n"))
        sys.stdout.flush()
    buf += chunk.replace(b"\r", b"\n")
    while b"\n" in buf:
        line, _, buf = buf.partition(b"\n")
        text = line.decode("utf-8", "replace")
        text = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
        if not text:
            continue
        with open("received.txt", "a") as f:
            f.write(text + "\n")
        if "--echo" in sys.argv:
            # Claude-style: repaint the submitted prompt into the transcript.
            print(f"❯ {text}", flush=True)
        print(f"len={len(text)}", flush=True)
        print("❯ ", flush=True)
"""


@pytest.fixture
def provider(tmp_path):
    class _Sessions:
        tmux_socket = f"aq-test-{tmp_path.name}"

    class _Cfg:
        data_dir = str(tmp_path / "state")
        sessions = _Sessions()

    return TmuxProvider(config=_Cfg())


@pytest.fixture
def stub_path(tmp_path) -> Path:
    path = tmp_path / "stub_agent.py"
    path.write_text(STUB, encoding="utf-8")
    return path


def _spec(
    tmp_path, stub_path, *, name="s-tm1", token="tok-1", dialog=False, echo=False, mute=False, **kw
) -> SessionSpec:
    command = [sys.executable, str(stub_path)]
    if dialog:
        command.append("--dialog")
    if echo:
        command.append("--echo")
    if mute:
        command.append("--mute")
    defaults = dict(
        session_name=name,
        work_dir=str(tmp_path / "wd"),
        command=tuple(command),
        env={"AQ_SESSION_ID": f"sess-{name}", "AQ_INSTANCE_TOKEN": token},
        prompt=None,
        prompt_mode="none",
        ready_prompt_prefix="❯ ",  # plain space: NBSP normalization under test
        process_names=("python",),
        instance_token=token,
    )
    defaults.update(kw)
    return SessionSpec(**defaults)


async def _received(tmp_path, tries=40) -> str:
    """Poll for the stub's received.txt (submission is asynchronous)."""
    path = Path(tmp_path) / "wd" / "received.txt"
    for _ in range(tries):
        if path.exists():
            return path.read_text(encoding="utf-8")
        await asyncio.sleep(0.1)
    return ""


class TestStartupReadiness:
    async def test_ready_prompt_with_nbsp_matches_plain_space_prefix(
        self, provider, tmp_path, stub_path
    ):
        handle = await provider.start(_spec(tmp_path, stub_path))
        try:
            assert await provider.is_running(handle) is True
            assert PROMPT.strip() in await provider.peek(handle)
        finally:
            await provider.stop(handle)

    async def test_startup_dialog_is_dismissed_before_ready(self, provider, tmp_path, stub_path):
        spec = _spec(
            tmp_path,
            stub_path,
            dialog=True,
            dialogs=(DialogRule(name="trust", pattern="Do you trust", keys=("y",)),),
        )
        handle = await provider.start(spec)
        try:
            text = await provider.peek(handle)
            assert PROMPT.strip() in text  # dialog answered, prompt reached
        finally:
            await provider.stop(handle)

    async def test_create_flags_actually_stick(self, provider, tmp_path, stub_path):
        # §9: these options are applied with errors suppressed, so a
        # target-syntax regression (e.g. "=name" vs "=name:") would fail
        # silently — pin them by reading the options back.
        handle = await provider.start(_spec(tmp_path, stub_path))
        try:
            for option, wanted in (("remain-on-exit", "on"), ("window-size", "latest")):
                out = await provider._tmux("show-options", "-w", "-t", f"={handle.name}:", option)
                assert wanted in out, f"{option} not applied: {out!r}"
        finally:
            await provider.stop(handle)

    async def test_instant_death_raises_with_captured_output(self, provider, tmp_path, stub_path):
        from src.sessions.provider import SessionDiedDuringStartup

        spec = _spec(
            tmp_path,
            stub_path,
            name="s-dead",
            command=(sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"),
        )
        with pytest.raises(SessionDiedDuringStartup):
            await provider.start(spec)
        assert await provider.list_running("s-dead") == []


class TestNudge:
    async def test_short_nudge_is_delivered_and_submitted(self, provider, tmp_path, stub_path):
        handle = await provider.start(_spec(tmp_path, stub_path))
        try:
            await provider.nudge(handle, "status check please")
            assert "status check please" in await _received(tmp_path)
        finally:
            await provider.stop(handle)

    async def test_echoed_submit_is_not_misread_as_unsubmitted(self, provider, tmp_path, stub_path):
        """Live-test regression: Claude repaints the submitted prompt into
        the transcript, so the marker stays on screen after a successful
        submit.  The confirm must not raise NotSubmitted for that — it
        caused the delivery engine to re-nudge the same envelope forever."""
        handle = await provider.start(_spec(tmp_path, stub_path, echo=True))
        try:
            await provider.nudge(handle, "please summarize the project state")
            received = await _received(tmp_path)
            assert received.count("please summarize the project state") == 1
        finally:
            await provider.stop(handle)

    async def test_swallowed_input_raises_not_submitted(self, provider, tmp_path, stub_path):
        """Live-test regression: a harness mid-turn can swallow typed keys
        entirely.  The old confirm read "marker absent" as submitted and
        the message was marked delivered without ever being seen.  Typed
        text must *land* (render in the pane) or the nudge fails so the
        delivery row stays pending for a retry."""
        from src.sessions.provider import NotSubmitted

        handle = await provider.start(_spec(tmp_path, stub_path, mute=True))
        try:
            with pytest.raises(NotSubmitted):
                await provider.nudge(handle, "you never saw this")
            assert "you never saw this" not in await _received(tmp_path, tries=3)
        finally:
            await provider.stop(handle)

    async def test_large_nudge_takes_the_paste_buffer_path(self, provider, tmp_path, stub_path):
        # >4096 bytes forces load-buffer + paste-buffer -p; a raw send-keys
        # of this size would also blow the canonical-mode line limit.
        big = "x" * 5000 + "-END"
        handle = await provider.start(_spec(tmp_path, stub_path))
        try:
            await provider.nudge(handle, big)
            got = await _received(tmp_path)
            assert big in got
        finally:
            await provider.stop(handle)

    async def test_nudge_does_not_ratchet_activity(self, provider, tmp_path, stub_path):
        import time

        handle = await provider.start(_spec(tmp_path, stub_path))
        try:
            before = await provider.last_activity(handle)
            sent = time.time()
            await provider.nudge(handle, "poke")
            just_after = await provider.last_activity(handle)
            if time.time() - sent > 2.5:
                pytest.skip("machine too loaded to observe the 3 s discount window")
            # Poke discounting: our own send reports the pre-send value.
            assert just_after == before
        finally:
            await provider.stop(handle)


class TestKillFencing:
    async def test_stop_kills_the_whole_process_tree(self, provider, tmp_path, stub_path):
        handle = await provider.start(_spec(tmp_path, stub_path))
        panes = await provider._panes()
        pane_pid = panes[handle.name].pid
        await provider.stop(handle)
        await asyncio.sleep(0.3)
        assert await proctable.read_start_ticks(pane_pid) is None
        assert await provider.is_running(handle) is False

    async def test_stale_token_stop_spares_a_name_reused_successor(
        self, provider, tmp_path, stub_path
    ):
        from src.sessions.provider import SessionHandle

        # Predecessor died; the same name now hosts a successor with a new
        # token.  The reconciler's stale handle must not touch it.
        live = await provider.start(_spec(tmp_path, stub_path, token="tok-new"))
        stale = SessionHandle(name=live.name, provider=provider.name, instance_token="tok-old")
        try:
            await provider.stop(stale)
            assert await provider.is_running(live) is True
            assert await provider.process_alive(live, ("python",)) is True
        finally:
            await provider.stop(live)


class TestLivenessContract:
    """``is_running`` and ``process_alive`` answer different questions.

    Pinned against a *real* tmux server on purpose: ``FakeProvider`` flips
    both at once on ``stop()``, so no fake can catch a caller that reaches
    for ``is_running`` when it means "did the agent finish".  Under
    ``remain-on-exit on`` (set by :meth:`TmuxProvider.start`) the pane
    outlives its process, and ``is_running`` keeps saying yes forever.
    """

    async def test_is_running_stays_true_after_the_process_exits(
        self, provider, tmp_path, stub_path
    ):
        handle = await provider.start(_spec(tmp_path, stub_path, name="s-live"))
        try:
            panes = await provider._panes()
            os.kill(panes[handle.name].pid, signal.SIGKILL)
            for _ in range(50):
                provider._cache.invalidate()
                if not await provider.process_alive(handle, ("python",)):
                    break
                await asyncio.sleep(0.1)
            # The process is gone...
            assert await provider.process_alive(handle, ("python",)) is False
            # ...but the pane (and therefore is_running) is not.
            assert await provider.is_running(handle) is True
        finally:
            await provider.stop(handle)


class TestAdoption:
    async def test_fresh_provider_instance_adopts_a_running_session(
        self, provider, tmp_path, stub_path
    ):
        """Simulated daemon restart: the tmux server owns the session."""
        handle = await provider.start(_spec(tmp_path, stub_path))
        await provider.set_meta(handle, "AQ_DRAIN_ACK", "1")

        reborn = TmuxProvider(config=provider.config)  # fresh caches, same socket
        try:
            found = await reborn.list_running("s-")
            assert [h.name for h in found] == [handle.name]
            adopted = found[0]
            # The instance token survives on the session environment…
            assert adopted.instance_token == handle.instance_token
            # …and so does provider-side meta (the drain-ack rides on this).
            assert await reborn.get_meta(adopted, "AQ_DRAIN_ACK") == "1"
            assert await reborn.process_alive(adopted, ("python",)) is True
        finally:
            await reborn.stop(handle)

    async def test_env_marker_scan_finds_the_agent_process(self, provider, tmp_path, stub_path):
        handle = await provider.start(_spec(tmp_path, stub_path, name="s-scan"))
        try:
            entries = await proctable.scan_by_env_marker("AQ_SESSION_ID")
            markers = {e.marker for e in entries}
            assert "sess-s-scan" in markers
        finally:
            await provider.stop(handle)


@pytest.mark.asyncio
async def test_peek_ansi_flag_adds_dash_e(monkeypatch):
    """ansi=True puts -e on capture-pane; ansi=False leaves it off."""
    from src.sessions.provider import SessionHandle
    from src.sessions.tmux import TmuxProvider

    provider = TmuxProvider(config=None)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args, **kwargs):
        calls.append(args)
        return "screen"

    async def fenced(_h):
        return True

    monkeypatch.setattr(provider, "_tmux", fake_tmux)
    monkeypatch.setattr(provider, "_fenced", fenced)
    handle = SessionHandle(name="s1", provider="tmux", instance_token="tok")

    await provider.peek(handle, 10, ansi=True)
    await provider.peek(handle, 10, ansi=False)

    assert "-e" in calls[0]
    assert "-e" not in calls[1]
    assert calls[0][0] == "capture-pane"
