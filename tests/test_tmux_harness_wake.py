"""Waking an idle worker: the real Codex and Claude CLIs in real tmux panes.

Live failure (2026-09-27, task steady-cascade): idle Codex workers never
acted on a supervisor message or a resolved wait.  Each pane showed the
daemon's own "No progress ... Close or continue" reminder typed into the
composer but never submitted, and every later message nudge deferred behind
it.  The fake composers in ``test_tmux_nudge_*`` encode what the screen
parse must accept; this module checks that against the CLIs themselves.

Each harness is launched exactly as its shipped definition says
(``src/sessions/default_harnesses/*.md``: prompt prefix, dialogs, Escape and
clear-key quirks) at tmux's default 80x24, so a ~430-character reminder
wraps.  The model API is a local fake that answers every turn with "ok", so
nothing leaves the machine and delivery is asserted on the request the CLI
actually sent, not by scraping the screen.  Configuration is hermetic: a
throwaway ``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR`` with a fake key, never the
operator's own login.

Marked ``tmux`` (deselected by default).  Run with::

    aq test tests/test_tmux_harness_wake.py -m tmux -p no:xdist
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

if os.name != "posix":  # pragma: no cover
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions import tmux as tmux_module
from src.sessions.harness_parser import parse_harness_markdown
from src.sessions.provider import SessionSpec
from src.sessions.tmux import TmuxProvider, _marker_for, _submit_pending

pytestmark = pytest.mark.tmux

HARNESSES = Path(__file__).resolve().parent.parent / "src" / "sessions" / "default_harnesses"

LONG_REMINDER = (
    "No progress for 12 min on task wise-ember.17. Close or continue: if the work is "
    'done run `aq task close wise-ember.17 --outcome pass|fail --summary "..."` then '
    "`aq session drain-ack`; if it is not done, keep working and run `aq task heartbeat "
    "wise-ember.17`; if you are blocked, say so with `aq message send --to user:dashboard "
    '--project "$AQ_PROJECT_ID" --body "Blocked: <question>"`.'
)
MESSAGE = "Handle `aq message status msg-9208c3d7479a4c188b8fea330ead76f0 --json`."
#: What each harness paints once its composer is idle and ready for input.
IDLE_FOOTER = {"codex": "for shortcuts", "claude": "bypass permissions on"}
#: A fake Anthropic key; Claude records approval by its last 20 characters.
FAKE_KEY = "sk-ant-api03-" + "x" * 80 + "AA"


def _sse(handler: BaseHTTPRequestHandler, events: list[tuple[str, dict]]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    for name, data in events:
        handler.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
    handler.wfile.flush()


def _anthropic_ok(model: str) -> list[tuple[str, dict]]:
    message = {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    return [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }),
        ("message_stop", {"type": "message_stop"}),
    ]


def _responses_ok() -> list[tuple[str, dict]]:
    item = {
        "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
    }
    usage = {
        "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2,
    }
    return [
        ("response.created", {"type": "response.created", "response": {"id": "resp_fake"}}),
        ("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        }),
        ("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": "msg_1",
            "output_index": 0, "content_index": 0, "delta": "ok",
        }),
        ("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0, "item": item,
        }),
        ("response.completed", {
            "type": "response.completed", "response": {"id": "resp_fake", "usage": usage},
        }),
    ]


class FakeModelAPI:
    """Answers every Anthropic Messages / OpenAI Responses turn with "ok".

    Keeps each request body so a test can assert what the CLI sent.
    """

    def __init__(self) -> None:
        self.bodies: list[object] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _reply(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._reply(404, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                api.bodies.append(body)
                path = self.path.split("?", 1)[0]
                self.close_connection = True
                if path.endswith("/messages/count_tokens"):
                    self._reply(200, {"input_tokens": 1})
                elif path.endswith("/messages") and body.get("stream"):
                    _sse(self, _anthropic_ok(body.get("model", "claude")))
                elif path.endswith("/messages"):
                    self._reply(200, {
                        "id": "msg_fake", "type": "message", "role": "assistant",
                        "model": body.get("model", "claude"),
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    })
                elif path.endswith("/responses"):
                    _sse(self, _responses_ok())
                else:
                    self._reply(404, {})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def received(self, text: str) -> bool:
        """Whether some request carried *text* verbatim in one string field."""

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)

        return any(text in value for body in list(self.bodies) for value in strings(body))


@pytest.fixture
def api():
    server = FakeModelAPI()
    yield server
    server.close()


def _reap(home: Path) -> None:
    """Kill anything still running out of *home*.

    Codex installs and starts a background app-server daemon under its
    ``CODEX_HOME``; it outlives the TUI (and a failed start leaves it
    half-up), so a test that only stops its pane leaks one per run.
    """
    prefix = str(home) + os.sep
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = os.readlink(entry / "exe")
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if exe.startswith(prefix) or any(arg.decode(errors="replace").startswith(prefix) for arg in argv):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def cli_home():
    """A short-pathed, throwaway CODEX_HOME / CLAUDE_CONFIG_DIR.

    Codex 0.157 opens a unix socket under its home, and pytest's
    ``tmp_path`` is long enough to exceed ``SUN_LEN``.
    """
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    home = Path(tempfile.mkdtemp(prefix="aqw-", dir=base))
    yield home
    _reap(home)
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def socket_name(tmp_path):
    name = f"aq-wake-{uuid.uuid4().hex[:10]}"
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, timeout=10, check=False
    )
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    Path(f"{tmpdir}/tmux-{os.getuid()}/{name}").unlink(missing_ok=True)


def _provider(socket_name: str, tmp_path: Path) -> TmuxProvider:
    class _Sessions:
        tmux_socket = socket_name

    class _Cfg:
        data_dir = str(tmp_path / "state")
        sessions = _Sessions()

    return TmuxProvider(config=_Cfg())


def _shipped(harness_id: str):
    parsed = parse_harness_markdown(
        (HARNESSES / f"{harness_id}.md").read_text(encoding="utf-8"), fallback_id=harness_id
    )
    assert parsed.is_valid, parsed.errors
    return parsed.harness


def _base_env() -> dict[str, str]:
    """A worker pane's environment minus anything naming a real account."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "TMPDIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    # Worker panes run with NO_COLOR (src/sessions/spec.py); the composer
    # guard must read the screens they actually paint.
    env["NO_COLOR"] = "1"
    return env


def _codex_spec(tmp_path: Path, api: FakeModelAPI, token: str, home: Path) -> SessionSpec:
    harness = _shipped("codex")
    work_dir = tmp_path / "codex-wd"
    work_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(work_dir)], check=True)
    (home / "config.toml").write_text(
        f"""
model = "gpt-6-sol"
model_provider = "fake"

[model_providers.fake]
name = "fake"
base_url = "{api.url}/v1"
wire_api = "responses"
env_key = "FAKE_MODEL_KEY"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0

[tui]
screen_reader_detection_done = true

[tui.model_availability_nux]
gpt-6-sol = 4

[notice]
hide_full_access_warning = true

[projects."{work_dir}"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    env = {**_base_env(), "CODEX_HOME": str(home), "FAKE_MODEL_KEY": "fake"}
    return _spec(harness, work_dir, env, token)


def _claude_spec(tmp_path: Path, api: FakeModelAPI, token: str, config: Path) -> SessionSpec:
    harness = _shipped("claude")
    work_dir = tmp_path / "claude-wd"
    work_dir.mkdir()
    (config / ".claude.json").write_text(
        json.dumps({
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "customApiKeyResponses": {"approved": [FAKE_KEY[-20:]], "rejected": []},
            "bypassPermissionsModeAccepted": True,
            "projects": {
                str(work_dir): {"hasTrustDialogAccepted": True, "allowedTools": []},
            },
        }),
        encoding="utf-8",
    )
    # The shipped --settings file disables prompt suggestions: ghost text in
    # an idle composer is indistinguishable from a draft under NO_COLOR.
    (config / "settings.json").write_text(
        json.dumps({"skipDangerousModePermissionPrompt": True, "promptSuggestionEnabled": False}),
        encoding="utf-8",
    )
    env = {
        **_base_env(),
        "CLAUDE_CONFIG_DIR": str(config),
        "ANTHROPIC_API_KEY": FAKE_KEY,
        "ANTHROPIC_BASE_URL": api.url,
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    return _spec(harness, work_dir, env, token)


def _spec(harness, work_dir: Path, env: dict[str, str], token: str) -> SessionSpec:
    command = (harness.command, *harness.args, harness.permission_flag)
    return SessionSpec(
        session_name=f"wake-{harness.id}",
        work_dir=str(work_dir),
        command=tuple(part for part in command if part),
        env={**env, "AQ_SESSION_ID": f"wake-{harness.id}", "AQ_INSTANCE_TOKEN": token},
        prompt=None,
        prompt_mode="none",
        ready_delay_ms=harness.ready_delay_ms,
        ready_prompt_prefix=harness.ready_prompt_prefix,
        process_names=harness.process_names,
        dialogs=harness.dialogs,
        skip_escape_before_enter=harness.skip_escape_before_enter,
        composer_clear_keys=harness.composer_clear_keys,
        instance_token=token,
    )


async def _until(predicate, *, timeout: float, what: str, screen=None) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            shown = f"; screen:\n{await screen()}" if screen is not None else ""
            raise AssertionError(f"timed out waiting for {what}{shown}")
        await asyncio.sleep(0.25)


async def _start_idle(provider: TmuxProvider, spec: SessionSpec, harness_id: str):
    handle = await provider.start(spec)
    footer = IDLE_FOOTER[harness_id]

    async def screen() -> str:
        return await provider.peek(handle, 30)

    async def idle() -> bool:
        return footer in await screen()

    await _until(idle, timeout=90, what=f"an idle {harness_id} composer", screen=screen)
    await asyncio.sleep(1.0)  # let the first frame settle before the guard reads it
    return handle


async def _delivered(api: FakeModelAPI, text: str) -> None:
    async def seen() -> bool:
        return api.received(text)

    await _until(seen, timeout=60, what=f"the model request carrying {text[:40]!r}")


SPECS = {"codex": _codex_spec, "claude": _claude_spec}


@pytest.fixture(params=sorted(SPECS))
def harness_id(request):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    if shutil.which(request.param) is None:
        pytest.skip(f"the {request.param} CLI is not installed")
    return request.param


class TestIdleWorkerWakes:
    async def test_a_wrapped_reminder_then_a_message_reach_the_model(
        self, harness_id, api, socket_name, tmp_path, cli_home
    ):
        provider = _provider(socket_name, tmp_path)
        token = f"tok-{uuid.uuid4().hex[:8]}"
        handle = await _start_idle(
            provider, SPECS[harness_id](tmp_path, api, token, cli_home), harness_id
        )
        try:
            await provider.nudge(handle, LONG_REMINDER)
            await _delivered(api, LONG_REMINDER)
            await provider.nudge(handle, MESSAGE)
            await _delivered(api, MESSAGE)
            assert await provider.pending_submit_detail(handle) is None
        finally:
            await provider.stop(handle)

    async def test_a_reminder_left_in_the_composer_is_submitted_by_the_next_wake(
        self, harness_id, api, socket_name, tmp_path, cli_home
    ):
        """What the pre-fix daemon left in every Codex pane: its reminder typed,
        a durable pending record, and no Enter.  A restarted daemon's next
        wake -- a supervisor message -- must deliver both."""
        provider = _provider(socket_name, tmp_path)
        token = f"tok-{uuid.uuid4().hex[:8]}"
        handle = await _start_idle(
            provider, SPECS[harness_id](tmp_path, api, token, cli_home), harness_id
        )
        try:
            record = tmux_module._PendingSubmit(
                instance_token=handle.instance_token,
                marker=_marker_for(LONG_REMINDER),
                text=LONG_REMINDER,
            )
            await provider._remember_pending(handle, record)
            await provider._tmux("send-keys", "-t", f"={handle.name}:", "-l", "--", LONG_REMINDER)

            prefix = await provider._ready_prefix_hint(handle.name)
            assert prefix.strip()

            async def painted() -> bool:
                return _submit_pending(await provider.peek(handle, 30), record.marker, prefix)

            await _until(painted, timeout=10, what="the typed reminder to be painted")

            restarted = _provider(socket_name, tmp_path)
            assert await restarted.pending_submit(handle) == record.marker
            assert not api.received(LONG_REMINDER)

            await restarted.nudge(handle, MESSAGE)
            await _delivered(api, LONG_REMINDER)
            await _delivered(api, MESSAGE)
            assert await restarted.pending_submit_detail(handle) is None
        finally:
            await provider.stop(handle)
