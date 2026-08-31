"""CLIClient ↔ generated-client integration (plan 11-12, API-3 status).

``CLIClient.execute()`` intentionally routes everything through
``/api/execute`` (implementation spec: the generated package is a dashboard
client, not the CLI transport).  The typed-dispatch machinery
(``_build_typed_dispatch`` / ``_execute_typed``) is dormant compatibility
code with no live caller — these tests are what keeps it from decaying
unnoticed: discovery must still find the shipped client's modules, typed
parsing must still run, and the generic fallback must still deliver the
command result when the typed layer cannot parse a response.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.cli.client as cli_client
from src.cli.client import CLIClient, _get_typed_dispatch

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = REPO_ROOT / "packages" / "aq-client"
CLIENT_PKG_DIR = CLIENT_DIR / "agent_queue_api_client"


def _import_repo_client():
    """Make ``agent_queue_api_client`` resolve to the repo checkout."""
    loaded = sys.modules.get("agent_queue_api_client")
    if loaded is not None and Path(loaded.__file__).parent == CLIENT_PKG_DIR:
        return loaded
    for name in [n for n in sys.modules if n.split(".")[0] == "agent_queue_api_client"]:
        del sys.modules[name]
    if str(CLIENT_DIR) not in sys.path:
        sys.path.insert(0, str(CLIENT_DIR))
    pkg = importlib.import_module("agent_queue_api_client")
    assert Path(pkg.__file__).parent == CLIENT_PKG_DIR
    return pkg


@pytest.mark.asyncio
async def test_typed_dispatch_discovers_shipped_client_and_falls_back_on_unparseable_response(
    monkeypatch,
):
    """Plan 11 / API-3: discovery works; unparseable typed responses fall back."""
    _import_repo_client()
    monkeypatch.setattr(cli_client, "_TYPED_DISPATCH", None)

    dispatch = _get_typed_dispatch()
    # The reflection walk over the shipped package still finds real
    # operations with their request models.
    assert "task_show" in dispatch
    mod, req_model = dispatch["task_show"]
    assert hasattr(mod, "asyncio")
    assert req_model.__name__ == "TaskShowRequest"
    assert hasattr(req_model, "to_dict")
    # It found substantially the whole surface, not a lucky handful.
    assert len(dispatch) > 100

    from agent_queue_api_client.client import Client

    client = CLIClient(base_url="http://test")

    # Generated client wired to a transport answering an UNDOCUMENTED status
    # — the typed parser returns None and CLIClient must fall back.
    typed_http = AsyncMock(spec=httpx.AsyncClient)
    undocumented = MagicMock(spec=httpx.Response)
    undocumented.status_code = 500
    undocumented.content = b"boom"
    undocumented.headers = {}
    typed_http.request.return_value = undocumented
    generated = Client(base_url="http://test", raise_on_unexpected_status=False)
    generated.set_async_httpx_client(typed_http)
    client._generated_client = generated

    fallback_resp = MagicMock(spec=httpx.Response)
    fallback_resp.status_code = 200
    fallback_resp.json = lambda: {"ok": True, "result": {"id": "t1", "title": "T"}}
    generic_http = AsyncMock(spec=httpx.AsyncClient)
    generic_http.post.return_value = fallback_resp
    client._http = generic_http

    result = await client._execute_typed("task_show", {"task_id": "t1"}, dispatch["task_show"])

    # Typed parsing was attempted against the real generated operation...
    assert typed_http.request.await_count == 1
    request_kwargs = typed_http.request.await_args.kwargs
    assert request_kwargs["url"] == "/api/task/show"
    assert request_kwargs["json"] == {"task_id": "t1"}
    # ...and the generic fallback delivered the command result.
    assert generic_http.post.await_count == 1
    assert generic_http.post.await_args.args[0] == "/api/execute"
    assert result == {"id": "t1", "title": "T"}


@pytest.mark.asyncio
async def test_connect_shares_bearer_headers_with_generated_client(monkeypatch):
    """Plan 12: one authenticated httpx client serves health, generic, typed."""
    _import_repo_client()
    monkeypatch.setenv("AQ_API_TOKEN", "aqs_test_token_123")

    health_resp = MagicMock(spec=httpx.Response)
    health_resp.raise_for_status = lambda: None
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = health_resp

    with patch.object(cli_client.httpx, "AsyncClient", return_value=mock_http) as ctor:
        client = CLIClient(base_url="http://test")
        await client.connect()

    # The one httpx client is constructed with the bearer header...
    assert ctor.call_count == 1
    assert ctor.call_args.kwargs["headers"] == {
        "Authorization": "Bearer aqs_test_token_123"
    }
    # ...the health probe used it...
    assert mock_http.get.await_args.args[0] == "/api/health"
    # ...and the generated client was handed the SAME instance, so typed
    # requests inherit the same auth without a second configuration path.
    assert client._generated_client is not None
    assert client._generated_client.get_async_httpx_client() is mock_http

    # Generic execution also posts through the shared client.
    exec_resp = MagicMock(spec=httpx.Response)
    exec_resp.status_code = 200
    exec_resp.json = lambda: {"ok": True, "result": {}}
    mock_http.post.return_value = exec_resp
    await client.execute("list_tasks", {})
    assert mock_http.post.await_args.args[0] == "/api/execute"

    await client.close()
    assert client._generated_client is None
