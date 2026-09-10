"""Pool claims tolerate daemon restarts without replaying ambiguous writes."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.exceptions import CommandResponseError, DaemonNotRunningError


def invoke_claim(side_effect, *, wait=60, clock=None, json_mode=True):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.side_effect = side_effect
    args = (["--json"] if json_mode else []) + ["task", "claim", "--next", "--wait", str(wait)]
    with (
        patch("src.cli.agent_surface._get_client", return_value=client),
        patch("src.cli.agent_surface.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("src.cli.agent_surface.time", SimpleNamespace(monotonic=Mock(side_effect=clock or [0, 1]))),
    ):
        result = CliRunner().invoke(cli, args, env={"AQ_SESSION_ID": "pool-test"})
    return result, client, sleep


def test_claim_retries_connection_refusal_then_returns_claim():
    result, client, sleep = invoke_claim([
        DaemonNotRunningError("http://localhost:8081"),
        {"result": "claimed", "task": {"id": "task-1"}, "claim_epoch": 3},
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["result"] == "claimed"
    assert client.execute.await_count == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.parametrize("json_mode", [True, False])
def test_claim_unavailable_at_deadline_never_offers_worker_daemon_start(json_mode):
    result, client, sleep = invoke_claim(
        [DaemonNotRunningError("http://localhost:8081")],
        clock=[0, 60], json_mode=json_mode,
    )
    assert result.exit_code == 3, result.output
    assert "Start the daemon?" not in result.output
    assert client.execute.await_count == 1
    sleep.assert_not_awaited()
    if json_mode:
        assert json.loads(result.stdout)["error"]["code"] == "daemon_unreachable"
    else:
        assert "do not exit the pool loop" in result.output


def test_claim_without_wait_does_not_retry():
    result, client, sleep = invoke_claim([DaemonNotRunningError("http://localhost:8081")], wait=0)
    assert result.exit_code == 3
    assert client.execute.await_count == 1
    sleep.assert_not_awaited()


def test_claim_ambiguous_response_is_not_replayed():
    result, client, sleep = invoke_claim([CommandResponseError("task_claim")])
    assert result.exit_code != 0
    assert client.execute.await_count == 1
    sleep.assert_not_awaited()
