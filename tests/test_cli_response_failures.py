"""Lost command replies never trigger automatic replay of a possible write."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from src.cli.client import CLIClient
from src.cli.exceptions import CommandResponseError, DaemonNotRunningError


@pytest.mark.parametrize(
    "failure",
    ["disconnect", "timeout", "write", "empty", "html", "list", "missing_ok", "false_http_success"],
)
async def test_command_reply_failure_preserves_unknown_outcome_without_retry(failure):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        # The server may have committed before any of these failures.
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("private transport detail", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail", request=request)
        if failure == "write":
            raise httpx.WriteError("private transport detail", request=request)
        if failure == "empty":
            return httpx.Response(200, content=b"")
        if failure == "html":
            return httpx.Response(503, text="<html>private proxy detail</html>")
        if failure == "list":
            return httpx.Response(200, json=[])
        if failure == "missing_ok":
            return httpx.Response(200, json={"result": {"task_id": "new-task"}})
        return httpx.Response(500, json={"ok": True, "result": {"task_id": "new-task"}})

    client = CLIClient(base_url="http://test")
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as transport:
        client._http = transport
        with pytest.raises(CommandResponseError) as caught:
            await client.execute("create_task", {"title": "test"})
    assert len(calls) == 1
    expected_kind = {
        "disconnect": "RemoteProtocolError",
        "timeout": "ReadTimeout",
        "write": "WriteError",
        "empty": "invalid_json",
        "html": "invalid_json",
        "list": "invalid_envelope",
        "missing_ok": "invalid_envelope",
        "false_http_success": "http_status_mismatch",
    }[failure]
    assert caught.value.details == {
        "outcome": "unknown",
        "automatic_retry": False,
        "failure_kind": expected_kind,
        **({"read_timeout_seconds": 30.0} if failure == "timeout" else {}),
    }
    assert "may have completed" in str(caught.value)
    assert "private" not in str(caught.value)


async def test_health_transport_failure_closes_client_before_command_submission(monkeypatch):
    def handler(request):
        raise httpx.RemoteProtocolError("closed during health", request=request)

    transport = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("src.cli.client.httpx.AsyncClient", lambda **kwargs: transport)
    client = CLIClient(base_url="http://test")
    with pytest.raises(DaemonNotRunningError):
        await client.connect()
    assert transport.is_closed
    assert client._http is None


@pytest.mark.parametrize("command", ["pool_status", "explain_task"])
async def test_fleet_diagnostics_allow_more_than_default_read_budget(command):
    calls = []

    def handler(request):
        calls.append(request)
        # The read budget must accommodate multiple per-project 10s views.
        assert request.extensions["timeout"]["read"] == 120.0
        return httpx.Response(200, json={"ok": True, "result": {"success": True}})

    client = CLIClient(base_url="http://test")
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as transport:
        client._http = transport
        assert await client.execute(command) == {"success": True}
    assert len(calls) == 1


@pytest.fixture
def command_http_server():
    """Real HTTP framing, so a subprocess exercises httpx's response reader."""
    commands = []
    release = threading.Event()
    mode = {"value": "fragmented"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.path == "/api/health"
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/api/execute"
            commands.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if mode["value"] == "timeout":
                # No response bytes until the client has exited on ReadTimeout.
                release.wait(30)
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            if mode["value"] == "truncated":
                # Declare a chunk larger than what arrives, then disconnect.
                self.wfile.write(b'40\r\n{"ok":true,')
            elif mode["value"] == "malformed":
                body = b'{"ok":true,'
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            else:
                body = json.dumps(
                    {"ok": True, "result": {"success": True, "label": "rapids 🌊"}},
                    ensure_ascii=False,
                ).encode()
                # Split JSON and the multibyte UTF-8 character across HTTP chunks.
                for byte in body:
                    self.wfile.write(b"1\r\n" + bytes([byte]) + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", commands, mode
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_fragmented_chunked_json_is_read_completely(command_http_server):
    url, commands, _mode = command_http_server
    async with CLIClient(base_url=url) as client:
        assert await client.execute("pool_status") == {"success": True, "label": "rapids 🌊"}
    assert commands == [{"command": "pool_status", "args": {}}]


@pytest.mark.parametrize(
    "argv,command",
    [
        (["pool", "status", "--project-id", "test"], "pool_status"),
        (["task", "explain", "--task-id", "test-task"], "explain_task"),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "failure,kind",
    [
        ("timeout", "ReadTimeout"),
        ("truncated", "RemoteProtocolError"),
        ("malformed", "invalid_json"),
    ],
)
def test_diagnostic_entry_point_exits_one_for_lost_http_reply(
    command_http_server, argv, command, as_json, failure, kind,
):
    url, commands, mode = command_http_server
    mode["value"] = failure
    # Invoke the real console entry point in a fresh interpreter. Shorten only
    # the test's read timeout; MockTransport does not enforce HTTP timeouts.
    code = (
        "from src.cli.client import _COMMAND_TIMEOUTS\n"
        f"_COMMAND_TIMEOUTS[{command!r}] = 0.1\n"
        "from src.cli.app import main\n"
        "main()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, *(["--json"] if as_json else []), *argv],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "AQ_API_URL": url},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(commands) == 1
    assert commands[0]["command"] == command
    assert "No complete command response was received" in proc.stdout
    assert kind in proc.stdout
    assert "Start the daemon?" not in proc.stdout
    assert "Traceback" not in proc.stderr
    if as_json:
        error = json.loads(proc.stdout)["error"]
        assert error["code"] == "command_error"
        assert error["details"]["outcome"] == "unknown"
        assert error["details"]["automatic_retry"] is False
        assert error["details"]["failure_kind"] == kind
        if failure == "timeout":
            assert error["details"]["read_timeout_seconds"] == 0.1


@pytest.mark.parametrize("as_json", [False, True])
def test_real_cli_reports_lost_response_once_without_start_prompt(monkeypatch, as_json):
    from src.cli.app import cli

    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.RemoteProtocolError("connection closed", request=request)

    async def connect(client):
        client._http = httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(CLIClient, "connect", connect)
    result = CliRunner().invoke(cli, (["--json"] if as_json else []) + ["task", "list"])
    assert result.exit_code == 1
    assert calls == ["/api/execute"]
    assert "may have completed" in result.output
    assert "Start the daemon?" not in result.output
    assert "Traceback" not in result.output
    if as_json:
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "command_error"
        assert error["details"]["outcome"] == "unknown"


async def test_unhealthy_http_response_is_actionable_and_closes_client(monkeypatch):
    transport = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="private body")),
    )
    monkeypatch.setattr("src.cli.client.httpx.AsyncClient", lambda **kwargs: transport)
    from src.cli.exceptions import CommandError

    client = CLIClient(base_url="http://test")
    with pytest.raises(CommandError, match="health check returned HTTP 503"):
        await client.connect()
    assert client._http is None
    assert transport.is_closed
