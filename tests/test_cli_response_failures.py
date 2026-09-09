"""Lost command replies never trigger automatic replay of a possible write."""

from __future__ import annotations

import json

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
    assert caught.value.details == {"outcome": "unknown", "automatic_retry": False}
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
