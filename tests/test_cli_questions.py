"""CLI question commands always use the daemon's scoped command surface."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.exceptions import ScopeDeniedError


@pytest.fixture(autouse=True)
def clean_legacy(monkeypatch):
    monkeypatch.delenv("AQ_JSON_LEGACY", raising=False)


def client(result=None):
    stub = AsyncMock()
    stub.__aenter__.return_value = stub
    stub.__aexit__.return_value = False
    stub.execute.return_value = result or {"id": "q-1", "state": "answered"}
    return stub


@pytest.mark.parametrize("args,command,params", [
    (["list"], "question_list", {}),
    (["list", "--project", "p1"], "question_list", {"project_id": "p1"}),
    (["answer", "q-1", "--body", "Keep it local"], "question_answer",
        {"question_id": "q-1", "body": "Keep it local"}),
    (["escalate", "q-1", "--reason", "Needs approval"], "question_escalate",
        {"question_id": "q-1", "reason": "Needs approval"}),
])
def test_question_cli_dispatches_only_expected_scoped_arguments(args, command, params):
    stub = client({"questions": [], "count": 0} if command == "question_list" else None)
    with patch("src.cli.questions._get_client", return_value=stub):
        result = CliRunner().invoke(cli, ["--json", "question", *args])
    assert result.exit_code == 0, result.output
    stub.execute.assert_awaited_once_with(command, params)
    assert json.loads(result.output)["schema_version"] == 1


def test_question_scope_denial_has_no_fallback_or_privilege_flags(monkeypatch):
    monkeypatch.setenv("AQ_API_TOKEN", "test-worker-token")
    stub = client()
    stub.execute.side_effect = ScopeDeniedError("question_answer", "Only a supervisor can answer")
    with patch("src.cli.questions._get_client", return_value=stub):
        result = CliRunner().invoke(cli, [
            "--json", "question", "answer", "q-1", "--body", "Unauthorized",
        ])
    assert result.exit_code == 4, result.output
    assert json.loads(result.output)["error"]["code"] == "out_of_scope"
    stub.execute.assert_awaited_once_with("question_answer", {
        "question_id": "q-1", "body": "Unauthorized",
    })


def test_question_list_renders_pending_state_and_literal_worker_text():
    stub = client({"questions": [{
        "id": "q-1", "agent_id": "a", "task_id": "t", "state": "human",
        "question": "Should I use [bold]literal[/] tags?",
    }], "count": 1})
    with patch("src.cli.questions._get_client", return_value=stub):
        result = CliRunner().invoke(cli, ["question", "list"])
    assert result.exit_code == 0, result.output
    assert "q-1" in result.output
    assert "human" in result.output
    assert "[bold]literal[/]" in result.output


def test_question_cli_transmits_worker_bearer_token_and_does_not_retry_without_it(monkeypatch):
    import httpx
    from src.cli import client as client_module

    monkeypatch.setenv("AQ_API_TOKEN", "test-worker-token")
    captured = []

    def respond(request):
        captured.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        assert request.url.path == "/api/execute"
        return httpx.Response(403, json={"error": "out of scope: ordinary worker"})
    actual_client = httpx.AsyncClient
    class IsolatedClient(actual_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(client_module.httpx, "AsyncClient", IsolatedClient)
    result = CliRunner().invoke(cli, [
        "--api-url", "http://test.invalid", "--json", "question", "answer",
        "q-1", "--body", "Unauthorized",
    ])
    assert result.exit_code == 4, result.output
    submitted = [request for request in captured if request.method == "POST"]
    assert len(submitted) == 1
    assert submitted[0].headers["Authorization"] == "Bearer test-worker-token"
    assert json.loads(submitted[0].content) == {
        "command": "question_answer", "args": {"question_id": "q-1", "body": "Unauthorized"},
    }
