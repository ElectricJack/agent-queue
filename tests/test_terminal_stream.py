"""Terminal WebSocket security, flow control and fixed-session lifecycle."""
import asyncio
import importlib.util
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, URL

from src.api.auth import RequestScope
from src.models import Agent, AgentState, SessionRecord
from tests.db_fixtures import lease_dsn


def module():
    assert importlib.util.find_spec("src.api.terminal_stream"), "live terminal router is required"
    from src.api import terminal_stream
    return terminal_stream


class Socket:
    def __init__(self, *, headers=None, host="127.0.0.1", query=None):
        self.headers = Headers(headers or {"host": "localhost:5173", "origin": "http://localhost:5173"})
        self.url = URL("ws://localhost:5173/ws/terminal/s")
        self.client = SimpleNamespace(host=host)
        self.query_params = query or {}
        self.scope = {"subprotocols": ["aq-terminal-v1"]}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = False
        self.closed = None

    async def accept(self, subprotocol=None):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = code
        await self.outgoing.put({"type": "closed", "code": code})

    async def send_json(self, message):
        await self.outgoing.put(message)

    async def send_bytes(self, data):
        await self.outgoing.put(data)

    async def receive(self):
        return await self.incoming.get()

    async def control(self, message):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(message)})

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def disconnect(self):
        await self.incoming.put({"type": "websocket.disconnect"})


class Client:
    def __init__(self):
        self.output = asyncio.Queue()
        self.inputs = []
        self.sizes = []
        self.closed = False
        self.valid = True
        self.reads = 0
        self.pending = b""
        self.input_attaches = []

    async def read(self, limit):
        self.reads += 1
        if not self.pending:
            self.pending = await self.output.get()
        data, self.pending = self.pending[:limit], self.pending[limit:]
        return data

    async def write(self, data):
        self.inputs.append(data)

    async def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    async def verify(self):
        return self.valid

    async def close(self):
        self.closed = True


@pytest.fixture
def setup():
    row = SessionRecord(
        id="s", project_id=None, profile_id="worker", harness="claude", provider="tmux",
        name="n-agent", lifecycle="named", state="running", work_dir="/work", epoch="e",
        instance_token="instance-a", started_at=time.time(), agent_id="a",
    )
    agent = Agent(id="a", name="A", profile_id="worker", state=AgentState.IDLE)
    db = SimpleNamespace(row=row, agent=agent, reads=0)
    async def get_session(sid):
        db.reads += 1
        return replace(db.row) if db.row is not None and db.row.id == sid else None
    async def get_agent(aid):
        return db.agent
    db.touches = []
    async def touch_session_activity(sid, timestamp):
        db.touches.append((sid, timestamp))
    db.touch_session_activity = touch_session_activity
    db.get_session = get_session
    db.get_agent = get_agent
    client = Client()
    async def attach(provider, row, *, cols, rows):
        client.sizes.append((cols, rows))
        return client
    async def attach_input(provider, row):
        client.input_attaches.append(row.name)
        return client
    config = SimpleNamespace(api_auth=SimpleNamespace(require_session_token=False, trusted_dashboard_origins=[]))
    store = SimpleNamespace(value=RequestScope(kind="session", session_id="admin", elevated=True), calls=0)
    async def validate(token, **kwargs):
        store.calls += 1
        return store.value if token == "aqs_valid" else None
    store.validate = validate
    orch = SimpleNamespace(db=db, session_providers=SimpleNamespace(create=lambda name: object()))
    return SimpleNamespace(
        db=db, client=client, attach=attach, attach_input=attach_input,
        config=config, store=store, orch=orch,
    )


def service(setup, **kwargs):
    return module().TerminalStreamService(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
        attach_input=setup.attach_input, recheck_seconds=0.02, **kwargs,
    )


@pytest.fixture
def host_shell_setup(setup, monkeypatch):
    from src.config import HostShellConfig
    from src.sessions.host_shell import HostShell

    setup.config.host_shell = HostShellConfig(enabled=True)
    setup.events = []
    shell = HostShell("aq-host-shell-1", "host-instance", time.time(), 0)

    async def get(name):
        return shell if name == shell.name else None

    async def log_event(event_type, **kwargs):
        setup.events.append((event_type, kwargs))

    setup.db.log_event = log_event
    monkeypatch.setattr(
        module().TerminalStreamService, "host_shells", lambda self: SimpleNamespace(get=get),
    )
    return setup


@pytest.mark.parametrize("input_only", [False, True])
@pytest.mark.parametrize("viewer", [None, "operator", "other"])
async def test_host_shell_connections_and_disconnects_are_audited(
    host_shell_setup, caplog, input_only, viewer,
):
    setup = host_shell_setup
    headers = {"host": "localhost:5173", "origin": "http://localhost:5173"}
    if viewer:
        peer = "192.168.1.9" if viewer == "other" else "::1"
        setup.config.host_shell.allow_remote = viewer == "other"
        headers.update({"x-aq-dashboard-viewer": viewer, "x-aq-dashboard-peer": peer})
        identity = (
            f"remote-dashboard-viewer (peer {peer})" if viewer == "other"
            else f"local-operator via dashboard (peer {peer})"
        )
    else:
        # Direct connections have no edge verdict; a peer header is not evidence.
        headers["x-aq-dashboard-peer"] = "forged-peer"
        identity = "local-operator (127.0.0.1)"
    ws = Socket(headers=headers)
    svc = service(setup)
    started_at = time.time()
    task = asyncio.create_task(svc.handle(ws, "aq-host-shell-1", input_only=input_only))
    try:
        assert (await ws.next())["type"] == "ready"
        assert len(setup.events) == 1
        await ws.incoming.put({"type": "websocket.receive", "bytes": b"private command\r"})
        await ws.control({"type": "ping"})
        assert await ws.next() == {"type": "pong"}
        await ws.disconnect()
        await asyncio.wait_for(task, 2)
    finally:
        await svc.shutdown()
    connected, disconnected = (
        ("input_connected", "input_disconnected") if input_only else ("attached", "detached")
    )
    assert [event for event, _ in setup.events] == [
        f"host_shell.{connected}", f"host_shell.{disconnected}",
    ]
    assert [json.loads(event["payload"]) for _, event in setup.events] == [
        {"name": "aq-host-shell-1", "identity": identity},
    ] * 2
    records = [record for record in caplog.records if record.name == "aq.audit.host_shell"]
    assert [record.getMessage() for record in records] == [
        f"host shell {event}: aq-host-shell-1 by {identity}" for event in (connected, disconnected)
    ]
    assert all(started_at <= record.created <= time.time() for record in records)
    assert "private command" not in caplog.text
    assert setup.client.closed and not svc._handlers


@pytest.mark.parametrize("input_only", [False, True])
@pytest.mark.parametrize("ending", ["shutdown", "backend_error", "invalid_control"])
async def test_host_shell_disconnect_is_audited_on_every_exit(
    host_shell_setup, input_only, ending,
):
    setup = host_shell_setup
    if ending == "backend_error":
        async def fail(*args, **kwargs):
            raise RuntimeError("private backend error")
        setup.attach = setup.attach_input = fail
    ws = Socket()
    svc = service(setup)
    task = asyncio.create_task(svc.handle(ws, "aq-host-shell-1", input_only=input_only))
    try:
        first = await ws.next()
        if ending == "backend_error":
            assert first["type"] == "error" and first["code"] == 1011
        else:
            assert first["type"] == "ready"
            if ending == "shutdown":
                await svc.shutdown()
            else:
                await ws.control({"type": "invalid"})
                assert (await ws.next())["code"] == 4400
        if ending == "shutdown":
            assert task.cancelled()
        else:
            await asyncio.wait_for(task, 2)
    finally:
        await svc.shutdown()
    assert [event for event, _ in setup.events] == (
        ["host_shell.input_connected", "host_shell.input_disconnected"] if input_only
        else ["host_shell.attached", "host_shell.detached"]
    )


@pytest.mark.parametrize("input_only", [False, True])
@pytest.mark.parametrize("case,code", [
    ("disabled", 4403), ("remote_disabled", 4403), ("bearer", 4403),
    ("bearer_protocol", 4403), ("origin", 4403), ("host", 4403),
    ("direct_remote", 4403), ("token_required", 4401), ("limit", 4429),
    ("missing", 4409),
])
async def test_host_shell_refusals_never_attach_or_audit(
    host_shell_setup, caplog, input_only, case, code,
):
    setup = host_shell_setup
    setup.config.host_shell.allow_remote = True
    ws = Socket()
    name = "aq-host-shell-1"
    if case == "disabled":
        setup.config.host_shell.enabled = False
    elif case == "remote_disabled":
        setup.config.host_shell.allow_remote = False
        ws.headers = Headers({"host": "localhost:5173", "x-aq-dashboard-viewer": "other"})
    elif case == "bearer":
        ws.headers = Headers({"host": "localhost:5173", "authorization": "Bearer aqs_valid"})
    elif case == "bearer_protocol":
        ws.scope["subprotocols"].append("aq-bearer.aqs_valid")
    elif case == "origin":
        ws.headers = Headers({"host": "localhost:5173", "origin": "http://evil.example"})
    elif case == "host":
        ws.headers = Headers({"host": "evil.example", "origin": "http://evil.example"})
    elif case == "direct_remote":
        ws.client.host = "192.168.1.9"
        ws.headers = Headers({"host": "localhost:5173", "x-aq-dashboard-viewer": "other"})
    elif case == "token_required":
        setup.config.api_auth.require_session_token = True
    elif case == "missing":
        name = "aq-host-shell-2"
    svc = service(setup, connection_limit=0 if case == "limit" else 16)
    await svc.handle(ws, name, input_only=input_only)
    assert not ws.accepted and ws.closed == code
    assert setup.client.sizes == setup.client.input_attaches == setup.events == []
    assert not [record for record in caplog.records if record.name == "aq.audit.host_shell"]


async def test_binary_input_output_and_resize_without_per_key_database_work(setup):
    ws = Socket()
    task = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await setup.client.output.put(b"\x1b[38;2;12;34;56mcolor\x1b[0m")
    output = await ws.next()
    assert output == b"\x1b[38;2;12;34;56mcolor\x1b[0m"
    await ws.control({"type": "ack", "bytes": len(output)})
    before = setup.db.reads
    for _ in range(10):
        await ws.incoming.put({"type": "websocket.receive", "bytes": b"hello\r"})
    await ws.control({"type": "resize", "cols": 120, "rows": 40})
    await asyncio.sleep(0.01)
    assert setup.client.inputs == [b"hello\r"] * 10
    assert setup.client.sizes[-1] == (120, 40)
    assert setup.db.reads == before
    await ws.disconnect()
    await asyncio.wait_for(task, 2)
    assert setup.client.closed


@pytest.mark.parametrize("headers,required,host", [
    ({"authorization": "Basic wrong"}, False, "127.0.0.1"),
    ({"authorization": "Bearer "}, False, "127.0.0.1"),
    ({"authorization": "Bearer invalid"}, False, "127.0.0.1"),
    ({}, True, "127.0.0.1"),
    ({"authorization": "Bearer aqs_valid"}, False, "192.0.2.1"),
    ({"origin": "http://evil.example"}, False, "127.0.0.1"),
    ({"origin": "null"}, False, "127.0.0.1"),
])
async def test_auth_and_origin_refusals_never_attach(setup, headers, required, host):
    setup.config.api_auth.require_session_token = required
    ws = Socket(headers={"host": "localhost:5173", "origin": "http://localhost:5173", **headers}, host=host)
    await service(setup).handle(ws, "s")
    assert not ws.accepted
    assert ws.closed in {4401, 4403}
    assert setup.client.sizes == []


@pytest.mark.parametrize("scope", [
    RequestScope(kind="session", session_id="worker", project_id="p"),
    RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True),
    RequestScope(kind="session", session_id="unassigned", project_id=None),
])
async def test_only_global_operator_scope_can_attach(setup, scope):
    setup.store.value = scope
    ws = Socket(headers={"host": "localhost:5173", "authorization": "Bearer aqs_valid"})
    await service(setup).handle(ws, "s")
    assert ws.closed == 4403 and not ws.accepted


async def test_bearer_subprotocol_and_trusted_proxy_origin(setup):
    setup.config.api_auth.require_session_token = True
    setup.config.api_auth.trusted_dashboard_origins = ["https://dashboard.example"]
    ws = Socket(headers={"host": "api.example", "origin": "https://dashboard.example"})
    ws.scope["subprotocols"] = ["aq-terminal-v1", "aq-bearer.aqs_valid"]
    task = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    setup.store.value = None
    assert (await ws.next())["type"] == "error"
    await asyncio.wait_for(task, 2)
    assert setup.client.closed


@pytest.mark.parametrize("change", ["stopped", "deleted", "instance"])
async def test_session_generation_and_definition_rechecked_after_attach(setup, change):
    ws = Socket()
    task = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    if change == "stopped":
        setup.db.row = replace(setup.db.row, state="stopped")
    elif change == "deleted":
        setup.db.agent.deleted_at = time.time()
    else:
        setup.db.row = replace(setup.db.row, instance_token="successor-token")
    assert (await ws.next())["type"] == "error"
    await asyncio.wait_for(task, 2)
    assert setup.client.closed and setup.client.inputs == []


async def test_instance_mismatch_before_ready_never_forwards_terminal_data(setup):
    setup.client.valid = False
    await setup.client.output.put(b"must not leak")
    ws = Socket()
    await service(setup).handle(ws, "s")
    assert (await ws.next())["type"] == "error"
    assert setup.client.reads == 0 and setup.client.closed


async def test_output_backpressure_waits_for_ack_without_dropping_bytes(setup):
    ws = Socket()
    task = asyncio.create_task(service(setup, output_limit=8).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await setup.client.output.put(b"0123456789abcdef")
    assert await ws.next() == b"01234567"
    await asyncio.sleep(0.05)
    assert ws.outgoing.empty()
    await ws.control({"type": "ack", "bytes": 8})
    assert await ws.next() == b"89abcdef"
    await ws.disconnect()
    await asyncio.wait_for(task, 2)


async def test_desktop_attach_sends_no_history_and_never_asks_to_restore_size(setup):
    calls = []
    async def attach(provider, row, **kwargs):
        calls.append(kwargs)
        return setup.client
    async def history(lines, rows):
        raise AssertionError("history was not requested")
    setup.client.history = history
    setup.attach = attach
    ws = Socket(query={"cols": "100", "rows": "30"})
    task = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    assert calls == [{"cols": 100, "rows": 30}]
    await ws.disconnect()
    await asyncio.wait_for(task, 2)


async def test_phone_history_precedes_attach_output_under_the_same_credit(setup):
    calls, asked = [], []
    async def attach(provider, row, **kwargs):
        calls.append(kwargs)
        return setup.client
    async def history(lines, rows):
        asked.append((lines, rows))
        return b"old-1\r\nold-2\r\n"
    setup.client.history = history
    setup.attach = attach
    await setup.client.output.put(b"live")
    ws = Socket(query={"cols": "45", "rows": "20", "history": "2000", "restore_size": "1"})
    task = asyncio.create_task(service(setup, output_limit=8).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    assert calls == [{"cols": 45, "rows": 20, "restore_size": True, "scrollback": True}]
    assert asked == [(2000, 20)]
    assert await ws.next() == b"old-1\r\no"
    await asyncio.sleep(0.05)
    assert ws.outgoing.empty() and setup.client.reads == 0
    await ws.control({"type": "ack", "bytes": 8})
    assert await ws.next() == b"ld-2\r\n"
    live = b""
    while live != b"live":
        chunk = await ws.next()
        live += chunk
        await ws.control({"type": "ack", "bytes": len(chunk)})
    await ws.disconnect()
    await asyncio.wait_for(task, 2)
    assert setup.client.closed


@pytest.mark.parametrize("query", [
    {"history": "-1"}, {"history": "10001"}, {"history": "1e3"}, {"history": "\u0661"},
    {"restore_size": "true"}, {"restore_size": "2"},
])
async def test_invalid_phone_options_refuse_before_attach(setup, query):
    ws = Socket(query={"cols": "80", "rows": "24", **query})
    await service(setup).handle(ws, "s")
    assert ws.closed == 4400 and not ws.accepted
    assert setup.client.sizes == []


@pytest.mark.parametrize("frame", [
    {"type": "ack", "bytes": 1},
    {"type": "resize", "cols": 100000, "rows": 20},
    {"type": "resize", "cols": True, "rows": 20},
    {"type": "unknown", "secret": "do not echo"},
])
async def test_malformed_controls_close_without_echoing_payload(setup, frame):
    ws = Socket()
    task = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await ws.control(frame)
    error = await ws.next()
    assert error["type"] == "error" and "do not echo" not in error["message"]
    await asyncio.wait_for(task, 2)
    assert setup.client.closed


async def test_stream_auth_can_refresh_persisted_revocation_beyond_cache():
    from src.api.auth import SessionTokenStore
    rows = {}
    async def insert_api_token(**row):
        rows[row["token_hash"]] = row
    async def get_api_token(key):
        return rows.get(key)
    store = SessionTokenStore(SimpleNamespace(insert_api_token=insert_api_token, get_api_token=get_api_token))
    token = await store.mint(session_id="admin", task_id=None, project_id=None, elevated=True)
    assert await store.validate(token)
    next(iter(rows.values()))["revoked_at"] = time.time()
    assert await store.validate(token, refresh=True) is None


@pytest.mark.parametrize("origin", ["http://localhost:5173#", "http://localhost:5173?", "http://localhost:5173/", "http://localhost:0"])
async def test_malformed_origin_cannot_bypass_check(setup, origin):
    ws = Socket(headers={"host": "localhost:5173", "origin": origin})
    await asyncio.wait_for(service(setup).handle(ws, "s"), 0.1)
    assert not ws.accepted and ws.closed == 4403


async def test_legacy_task_claim_epoch_change_disconnects_same_worker(setup):
    from src.models import TaskStatus
    task_row = SimpleNamespace(id="t", project_id="p", assigned_agent_id="a", status=TaskStatus.IN_PROGRESS, claim_epoch=1)
    setup.db.row = replace(setup.db.row, task_id="t", project_id="p", lifecycle="task", last_claim_epoch=None)
    setup.db.agent.current_task_id = "t"
    async def get_task(tid):
        return task_row
    setup.db.get_task = get_task
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    task_row.claim_epoch = 2
    assert (await ws.next())["type"] == "error"
    await asyncio.wait_for(running, 2)
    assert setup.client.closed


async def test_database_instance_change_during_attach_never_reaches_ready(setup):
    async def attach(*args, **kwargs):
        setup.db.row = replace(setup.db.row, instance_token="replacement")
        return setup.client
    setup.attach = attach
    ws = Socket()
    await service(setup).handle(ws, "s")
    assert (await ws.next())["type"] == "error"
    assert setup.client.closed and setup.client.reads == 0


async def test_output_ack_timeout_detaches_instead_of_dropping(setup):
    ws = Socket()
    running = asyncio.create_task(service(setup, output_limit=4, ack_timeout=0.03).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await setup.client.output.put(b"abcdefgh")
    assert await ws.next() == b"abcd"
    assert (await ws.next())["type"] == "error"
    await asyncio.wait_for(running, 2)
    assert setup.client.pending == b"efgh" and setup.client.closed


async def test_input_backpressure_is_bounded_and_not_echoed(setup):
    async def blocked_write(data):
        await asyncio.Event().wait()
    setup.client.write = blocked_write
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    for _ in range(3):
        await ws.incoming.put({"type": "websocket.receive", "bytes": b"s" * 65536})
    assert (await ws.next())["type"] == "error"
    await asyncio.wait_for(running, 2)
    assert setup.client.closed


async def test_shutdown_detaches_active_client(setup):
    stream = service(setup)
    ws = Socket()
    running = asyncio.create_task(stream.handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await stream.shutdown()
    assert running.done() and setup.client.closed and not stream._handlers


async def test_fastapi_websocket_route_negotiates_and_transports_raw_bytes(setup):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(module().build_terminal_router(setup.orch, setup.config, token_store=setup.store, attach=setup.attach))
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "ws", "path": "/ws/terminal/s",
        "raw_path": b"/ws/terminal/s", "query_string": b"cols=100&rows=35", "root_path": "",
        "headers": [(b"host", b"localhost:5173"), (b"origin", b"http://localhost:5173")],
        "client": ("127.0.0.1", 1234), "server": ("localhost", 5173),
        "subprotocols": ["aq-terminal-v1"], "state": {},
    }
    running = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    accepted = await asyncio.wait_for(outgoing.get(), 2)
    assert accepted == {"type": "websocket.accept", "subprotocol": "aq-terminal-v1", "headers": []}
    ready = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
    assert ready == {"type": "ready", "session_id": "s", "cols": 100, "rows": 35}
    await setup.client.output.put(b"\x1b[38;2;1;2;3mRGB")
    data = await asyncio.wait_for(outgoing.get(), 2)
    assert data == {"type": "websocket.send", "bytes": b"\x1b[38;2;1;2;3mRGB"}
    await incoming.put({"type": "websocket.receive", "bytes": b"x\r"})
    await asyncio.sleep(0.01)
    assert setup.client.inputs == [b"x\r"]
    await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(running, 2)
    assert setup.client.closed


@pytest.mark.parametrize("protocols", [
    ["aq-terminal-v1", "aq-bearer"],
    ["aq-terminal-v1", "aq-bearer."],
    ["aq-terminal-v1", "aq-bearer.aqs_valid", "aq-bearer.aqs_valid"],
])
async def test_malformed_or_duplicate_bearer_protocols_never_become_local(setup, protocols):
    ws = Socket()
    ws.scope["subprotocols"] = protocols
    await asyncio.wait_for(service(setup).handle(ws, "s"), 0.1)
    assert not ws.accepted and ws.closed == 4401 and setup.client.sizes == []


@pytest.mark.parametrize("unavailable", ["deleted", "stopped", "missing"])
async def test_unavailable_initial_session_does_not_attach(setup, unavailable):
    if unavailable == "deleted":
        setup.db.agent.deleted_at = time.time()
    elif unavailable == "stopped":
        setup.db.row = replace(setup.db.row, state="stopped")
    else:
        setup.db.row = None
    ws = Socket()
    await service(setup).handle(ws, "s")
    assert not ws.accepted and setup.client.sizes == []


async def test_waiting_input_live_task_terminal_remains_interactive(setup):
    from src.models import TaskStatus
    task_row = SimpleNamespace(id="t", project_id="p", assigned_agent_id="a", status=TaskStatus.WAITING_INPUT, claim_epoch=1)
    setup.db.row = replace(setup.db.row, task_id="t", project_id="p", lifecycle="task", last_claim_epoch=1)
    setup.db.agent.current_task_id = "t"
    async def get_task(tid):
        return task_row
    setup.db.get_task = get_task
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await ws.incoming.put({"type": "websocket.receive", "bytes": b"approve\r"})
    await asyncio.sleep(0.01)
    assert setup.client.inputs == [b"approve\r"]
    await ws.disconnect()
    await asyncio.wait_for(running, 2)


async def test_terminal_eof_sends_exit_and_detaches(setup):
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await setup.client.output.put(b"")
    assert await ws.next() == {"type": "exit"}
    await asyncio.wait_for(running, 2)
    assert setup.client.closed


async def test_safe_attach_error_is_actionable_but_generic_errors_are_private(setup):
    from src.sessions.terminal_pty import TerminalAttachError
    for failure, expected in [
        (TerminalAttachError("Terminal requires tmux detach-on-destroy on"), "detach-on-destroy"),
        (ValueError("sensitive command or environment"), "Terminal connection failed"),
    ]:
        async def attach(*args, **kwargs):
            raise failure
        setup.attach = attach
        ws = Socket()
        await service(setup).handle(ws, "s")
        error = await ws.next()
        assert error["type"] == "error" and expected in error["message"]
        assert "sensitive" not in error["message"]


async def test_silent_input_activity_is_touched_once_per_monitor_not_per_keystroke(setup):
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    before = time.time()
    for _ in range(10):
        await ws.incoming.put({"type": "websocket.receive", "bytes": b"x"})
    await asyncio.sleep(0.01)
    assert setup.client.inputs == [b"x"] * 10 and setup.db.touches == []
    await asyncio.sleep(0.025)
    assert len(setup.db.touches) == 1
    assert setup.db.touches[0][0] == "s" and setup.db.touches[0][1] >= before
    await asyncio.sleep(0.035)
    assert len(setup.db.touches) == 1
    await ws.disconnect()
    await asyncio.wait_for(running, 2)


async def test_blocked_websocket_send_cannot_pin_attached_client_forever(setup):
    ws = Socket()
    async def blocked_send(data):
        await asyncio.Event().wait()
    ws.send_bytes = blocked_send
    running = asyncio.create_task(service(setup, ack_timeout=0.03).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await setup.client.output.put(b"output")
    await asyncio.wait_for(running, 0.3)
    assert setup.client.closed
    assert (await ws.next())["type"] == "error"


async def test_dns_rebinding_host_origin_does_not_gain_local_terminal_access(setup):
    ws = Socket(headers={"host": "untrusted.example", "origin": "http://untrusted.example"})
    await asyncio.wait_for(service(setup).handle(ws, "s"), 0.1)
    assert not ws.accepted and ws.closed == 4403
    assert setup.client.sizes == []


async def test_explicit_trusted_custom_hostname_can_attach(setup):
    setup.config.api_auth.trusted_dashboard_origins = ["https://dashboard.example"]
    ws = Socket(headers={"host": "dashboard.example", "origin": "https://dashboard.example"})
    ws.url = URL("wss://dashboard.example/ws/terminal/s")
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await ws.disconnect()
    await asyncio.wait_for(running, 2)


async def test_older_tmux_observation_cannot_overwrite_silent_input_activity(setup, tmp_path):
    from src.database import Database
    db = Database(lease_dsn("activity.db"))
    await db.initialize()
    try:
        await db.create_session(replace(setup.db.row, agent_id=None, last_activity=None))
        await db.touch_session_activity("s", 100.0)
        await db.touch_session_activity("s", 200.0)  # successful silent input
        await db.touch_session_activity("s", 150.0)  # delayed tmux observation
        assert (await db.get_session("s")).last_activity == 200.0
        await db.touch_session_activity("s", 250.0)
        assert (await db.get_session("s")).last_activity == 250.0
    finally:
        await db.close()


async def test_ping_pong_is_not_input_or_activity(setup):
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s"))
    assert (await ws.next())["type"] == "ready"
    await ws.control({"type": "ping"})
    assert await ws.next() == {"type": "pong"}
    assert setup.client.inputs == []
    assert setup.db.touches == []
    await ws.disconnect()
    await asyncio.wait_for(running, 2)


@pytest.mark.parametrize("case,code", [
    ("live", 0), ("auth", 4401), ("origin", 4403),
    ("peer", 4403), ("ended", 4409), ("capacity", 4429),
])
async def test_http_terminal_probe_checks_access_without_attaching(setup, case, code):
    from starlette.requests import Request
    from starlette.responses import Response

    headers = [(b"host", b"localhost:5173")]
    if case == "auth":
        setup.config.api_auth.require_session_token = True
    elif case == "origin":
        # Same-origin browser GETs omit Origin; an untrusted Host still fails.
        headers = [(b"host", b"attacker.example")]
    elif case == "ended":
        setup.db.row = replace(setup.db.row, state="stopped")
    router = module().build_terminal_router(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
        connection_limit=0 if case == "capacity" else 16,
    )
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "methods", None) == {"GET"})
    request = Request({
        "type": "http", "method": "GET", "scheme": "http", "path": "/ws/terminal/s",
        "query_string": b"", "headers": headers,
        "server": ("localhost", 5173), "client": ("remote" if case == "peer" else "127.0.0.1", 1),
    })
    response = Response()
    result = await endpoint(request=request, session_id="s", response=response)
    assert response.headers["Cache-Control"] == "no-store"
    assert result.code == code
    assert result.status == ("ready" if code == 0 else "exited" if code == 4409 else "error")
    assert result.retryable == (code == 4429)
    assert setup.client.sizes == []  # attach() was never called
    assert setup.client.inputs == []
    assert not setup.client.closed


async def test_terminal_probe_trusted_tls_origin_and_expired_credentials(setup):
    from starlette.requests import Request
    from starlette.responses import Response

    setup.config.api_auth.trusted_dashboard_origins = ["https://dashboard.example"]
    setup.config.api_auth.require_session_token = True
    router = module().build_terminal_router(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
    )
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "methods", None) == {"GET"})
    request = Request({
        "type": "http", "method": "GET", "scheme": "http", "path": "/ws/terminal/s",
        "query_string": b"", "headers": [
            (b"host", b"dashboard.example"), (b"authorization", b"Bearer aqs_valid"),
        ], "server": ("localhost", 8081), "client": ("127.0.0.1", 1),
    })
    result = await endpoint(
        request=request, session_id="s", response=Response(),
        browser_origin="https://dashboard.example",
    )
    assert result.status == "ready"
    setup.store.value = None
    result = await endpoint(
        request=request, session_id="s", response=Response(),
        browser_origin="https://dashboard.example",
    )
    assert result.code == 4401 and not result.retryable
    assert setup.client.sizes == []


@pytest.mark.parametrize("lan", [False, True])
async def test_terminal_probe_and_keepalive_through_real_proxy(setup, tmp_path, lan):
    import aiohttp
    from fastapi import FastAPI
    from websockets.asyncio.client import connect

    from src.dashboard_server.app import create_app
    from src.dashboard_server.settings import DashboardServerSettings
    from tests.dashboard_server_helpers import serve_asgi
    from tests.test_dashboard_server_app import stage_bundle

    origin = "http://192.168.1.69:5173"
    if lan:
        setup.config.api_auth.trusted_dashboard_origins = [origin]
    app = FastAPI()
    app.include_router(module().build_terminal_router(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
    ))
    async with serve_asgi(app) as daemon_url:
        dashboard = create_app(DashboardServerSettings(
            host="0.0.0.0", api_url=daemon_url, bundle_directory=stage_bundle(tmp_path),
            trusted_origins=(origin,) if lan else (),
        ))

        async def relay(scope, receive, send):
            if lan and scope["type"] in {"http", "websocket"}:
                scope = {**scope, "client": ("172.29.48.1", 50000)}
                scope["headers"] = [
                    (key, b"192.168.1.69:5173" if key == b"host" else value)
                    for key, value in scope["headers"]
                ]
            await dashboard(scope, receive, send)

        try:
            async with serve_asgi(relay) as url, aiohttp.ClientSession() as http:
                params = {"browser_origin": origin} if lan else {}
                async with http.get(f"{url}/ws/terminal/s", params=params) as response:
                    assert response.status == 200
                    assert (await response.json())["status"] == "ready"
                    assert response.headers["Cache-Control"] == "no-store"
                assert setup.client.sizes == []
                async with connect(
                    url.replace("http://", "ws://") + "/ws/terminal/s?cols=80&rows=24",
                    origin=origin if lan else url, subprotocols=["aq-terminal-v1"], proxy=None,
                ) as ws:
                    assert json.loads(await asyncio.wait_for(ws.recv(), 2))["type"] == "ready"
                    await ws.send(json.dumps({"type": "ping"}))
                    assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"type": "pong"}
                    assert setup.client.inputs == []
                    assert setup.db.touches == []
        finally:
            await dashboard.proxy.close()


@pytest.mark.parametrize("input_only", [False, True])
async def test_host_shell_audit_uses_edge_peer_instead_of_forged_headers(
    host_shell_setup, tmp_path, caplog, input_only,
):
    from fastapi import FastAPI
    from websockets.asyncio.client import connect

    from src.dashboard_server.app import create_app
    from src.dashboard_server.settings import DashboardServerSettings
    from tests.dashboard_server_helpers import serve_asgi
    from tests.test_dashboard_server_app import stage_bundle

    setup = host_shell_setup
    origin = "http://192.168.1.69:5173"
    setup.config.host_shell.allow_remote = True
    setup.config.api_auth.trusted_dashboard_origins = [origin]
    disconnected = asyncio.Event()
    record_event = setup.db.log_event

    async def log_event(event_type, **kwargs):
        await record_event(event_type, **kwargs)
        if event_type in {"host_shell.detached", "host_shell.input_disconnected"}:
            disconnected.set()

    setup.db.log_event = log_event
    app = FastAPI()
    app.include_router(module().build_terminal_router(
        setup.orch, setup.config, attach=setup.attach, attach_input=setup.attach_input,
    ))
    async with serve_asgi(app) as daemon_url:
        dashboard = create_app(DashboardServerSettings(
            host="192.168.1.69", api_url=daemon_url, bundle_directory=stage_bundle(tmp_path),
        ))

        async def remote_peer(scope, receive, send):
            if scope["type"] in {"http", "websocket"}:
                scope = {**scope, "client": ("192.168.1.9", 50000), "headers": [
                    (key, b"192.168.1.69:5173" if key.lower() == b"host" else value)
                    for key, value in scope["headers"]
                ]}
            await dashboard(scope, receive, send)

        try:
            async with serve_asgi(remote_peer) as url:
                path = "/ws/terminal/aq-host-shell-1" + ("/input" if input_only else "")
                async with connect(
                    url.replace("http://", "ws://") + path, origin=origin,
                    subprotocols=["aq-terminal-v1"], proxy=None, additional_headers=[
                        ("x-aq-dashboard-viewer", "operator"),
                        ("x-aq-dashboard-peer", "203.0.113.10"),
                        ("X-AQ-Dashboard-Peer", "127.0.0.1"),
                    ],
                ) as ws:
                    assert json.loads(await asyncio.wait_for(ws.recv(), 2))["type"] == "ready"
                    assert len(setup.events) == 1
                await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            await dashboard.proxy.close()
    assert [event for event, _ in setup.events] == (
        ["host_shell.input_connected", "host_shell.input_disconnected"] if input_only
        else ["host_shell.attached", "host_shell.detached"]
    )
    identity = "remote-dashboard-viewer (peer 192.168.1.9)"
    assert [json.loads(event["payload"]) for _, event in setup.events] == [
        {"name": "aq-host-shell-1", "identity": identity},
    ] * 2
    records = [record for record in caplog.records if record.name == "aq.audit.host_shell"]
    assert len(records) == 2 and all(identity in record.getMessage() for record in records)
    assert "203.0.113.10" not in caplog.text


# -- Input-only terminal socket (/ws/terminal/{id}/input): phones type, never attach.


def _no_pty_attach(setup):
    async def attach(*args, **kwargs):
        raise AssertionError("the input-only socket must never create a tmux attach client")
    setup.attach = attach


@pytest.mark.parametrize("headers,required,host", [
    ({"authorization": "Basic wrong"}, False, "127.0.0.1"),
    ({"authorization": "Bearer invalid"}, False, "127.0.0.1"),
    ({}, True, "127.0.0.1"),
    ({"authorization": "Bearer aqs_valid"}, False, "192.0.2.1"),
    ({"origin": "http://evil.example"}, False, "127.0.0.1"),
    ({"origin": "null"}, False, "127.0.0.1"),
])
async def test_input_route_auth_and_origin_refusals_never_create_a_client(setup, headers, required, host):
    setup.config.api_auth.require_session_token = required
    _no_pty_attach(setup)
    ws = Socket(headers={"host": "localhost:5173", "origin": "http://localhost:5173", **headers}, host=host)
    await service(setup).handle(ws, "s", input_only=True)
    assert not ws.accepted
    assert ws.closed in {4401, 4403}
    assert setup.client.input_attaches == [] and setup.client.sizes == []


@pytest.mark.parametrize("unavailable", ["deleted", "stopped", "missing", "scope"])
async def test_input_route_session_and_operator_refusals_never_create_a_client(setup, unavailable):
    _no_pty_attach(setup)
    headers = None
    if unavailable == "deleted":
        setup.db.agent.deleted_at = time.time()
    elif unavailable == "stopped":
        setup.db.row = replace(setup.db.row, state="stopped")
    elif unavailable == "missing":
        setup.db.row = None
    else:
        setup.store.value = RequestScope(kind="session", session_id="worker", project_id="p")
        headers = {"host": "localhost:5173", "authorization": "Bearer aqs_valid"}
    ws = Socket(headers=headers)
    await service(setup).handle(ws, "s", input_only=True)
    assert not ws.accepted and setup.client.input_attaches == []


async def test_input_mode_ready_frame_has_no_dimensions_and_input_reaches_client(setup):
    _no_pty_attach(setup)
    # Dimensions are neither required nor parsed: an input socket has no size.
    ws = Socket(query={"cols": "nope"})
    running = asyncio.create_task(service(setup).handle(ws, "s", input_only=True))
    assert await ws.next() == {"type": "ready", "session_id": "s", "mode": "input"}
    assert setup.client.input_attaches == ["n-agent"]
    await ws.incoming.put({"type": "websocket.receive", "bytes": b"yes\r"})
    await ws.incoming.put({"type": "websocket.receive", "bytes": b"\x1b"})
    await ws.control({"type": "ping"})
    assert await ws.next() == {"type": "pong"}
    assert setup.client.inputs == [b"yes\r", b"\x1b"]
    assert setup.client.sizes == []
    await ws.disconnect()
    await asyncio.wait_for(running, 2)
    assert setup.client.closed and ws.outgoing.empty()


@pytest.mark.parametrize("frame", [
    {"type": "resize", "cols": 45, "rows": 30},
    {"type": "ack", "bytes": 1},
])
async def test_input_mode_refuses_resize_and_ack_with_4400(setup, frame):
    _no_pty_attach(setup)
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s", input_only=True))
    assert (await ws.next())["mode"] == "input"
    await ws.control(frame)
    error = await ws.next()
    assert error["type"] == "error" and error["code"] == 4400
    await asyncio.wait_for(running, 2)
    assert ws.closed == 4400
    assert setup.client.sizes == [] and setup.client.closed


@pytest.mark.parametrize("change", ["stopped", "deleted", "instance", "client"])
async def test_input_mode_generation_change_disconnects(setup, change):
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s", input_only=True))
    assert (await ws.next())["type"] == "ready"
    if change == "stopped":
        setup.db.row = replace(setup.db.row, state="stopped")
    elif change == "deleted":
        setup.db.agent.deleted_at = time.time()
    elif change == "instance":
        setup.db.row = replace(setup.db.row, instance_token="successor-token")
    else:
        setup.client.valid = False
    error = await ws.next()
    assert error["type"] == "error" and error["code"] == 4409
    await asyncio.wait_for(running, 2)
    assert setup.client.closed and setup.client.inputs == []


async def test_input_mode_refused_input_closes_4400_without_echo(setup):
    from src.sessions.terminal_input import TerminalInputError

    async def refuse(data):
        raise TerminalInputError("Terminal paste is too large.")
    setup.client.write = refuse
    ws = Socket()
    running = asyncio.create_task(service(setup).handle(ws, "s", input_only=True))
    assert (await ws.next())["type"] == "ready"
    await ws.incoming.put({"type": "websocket.receive", "bytes": b"\x1b[200~secret"})
    error = await ws.next()
    assert error == {
        "type": "error", "message": "Terminal paste is too large.", "code": 4400, "retryable": False,
    }
    await asyncio.wait_for(running, 2)
    assert ws.closed == 4400


async def test_fastapi_input_route_negotiates_and_transports_raw_bytes(setup):
    from fastapi import FastAPI
    _no_pty_attach(setup)
    app = FastAPI()
    app.include_router(module().build_terminal_router(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
        attach_input=setup.attach_input,
    ))
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "ws", "path": "/ws/terminal/s/input",
        "raw_path": b"/ws/terminal/s/input", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost:5173"), (b"origin", b"http://localhost:5173")],
        "client": ("127.0.0.1", 1234), "server": ("localhost", 5173),
        "subprotocols": ["aq-terminal-v1"], "state": {},
    }
    running = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    accepted = await asyncio.wait_for(outgoing.get(), 2)
    assert accepted == {"type": "websocket.accept", "subprotocol": "aq-terminal-v1", "headers": []}
    ready = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
    assert ready == {"type": "ready", "session_id": "s", "mode": "input"}
    await incoming.put({"type": "websocket.receive", "bytes": b"\x03"})
    await asyncio.sleep(0.01)
    assert setup.client.inputs == [b"\x03"]
    assert setup.client.input_attaches == ["n-agent"] and setup.client.sizes == []
    await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(running, 2)
    assert setup.client.closed and outgoing.empty()


# -- TmuxInputClient against a recording fake tmux (the real one: test_terminal_input.py).


class FakeTmux:
    def __init__(self):
        self.session_id = "$7"
        self.token = "instance-a"
        self.calls = []
        self.fail = None
        self.in_mode = "0"

    async def _tmux(self, *args, timeout=30.0, stdin=None):
        if args[0] == "display-message":
            return (self.in_mode if args[-1] == "#{pane_in_mode}" else self.session_id) + "\n"
        if args[0] == "show-environment":
            return f"AQ_INSTANCE_TOKEN={self.token}\n"
        self.calls.append((args, stdin))
        if args[0] == self.fail:
            raise RuntimeError(f"tmux {' '.join(args)} -> 1: typed secret")
        return ""


async def _input_client(tmux=None):
    from src.sessions.terminal_input import TmuxInputClient
    tmux = tmux or FakeTmux()
    row = SimpleNamespace(name="n-agent", instance_token="instance-a")
    return tmux, await TmuxInputClient.attach(tmux, row)


async def test_input_client_sends_raw_keys_as_hex_and_single_arrows_by_name():
    tmux, client = await _input_client()
    await client.write(b"hi\xe2\x82\xac\x03\x1b\r")
    await client.write(b"\x1b[A")
    await client.write(b"\x1bOD")
    await client.write(b"x\x1b[B")  # not exactly one arrow: raw bytes
    assert [args for args, _ in tmux.calls] == [
        ("send-keys", "-t", "$7", "-H", "68", "69", "e2", "82", "ac", "03", "1b", "0d"),
        ("send-keys", "-t", "$7", "Up"),
        ("send-keys", "-t", "$7", "Left"),
        ("send-keys", "-t", "$7", "-H", "78", "1b", "5b", "42"),
    ]
    tmux.calls.clear()
    await client.write(b"a" * 2500)
    assert [len(args) - 4 for args, _ in tmux.calls] == [1024, 1024, 452]


async def test_input_client_leaves_copy_mode_before_typing():
    tmux, client = await _input_client()
    tmux.in_mode = "1"
    await client.write(b"y")
    assert [args for args, _ in tmux.calls] == [
        ("send-keys", "-t", "$7", "-X", "cancel"),
        ("send-keys", "-t", "$7", "-H", "79"),
    ]


async def test_input_client_bracketed_paste_uses_a_unique_buffer_even_across_frames():
    tmux, client = await _input_client()
    await client.write(b"ab\x1b[200~line1\nli")
    await client.write(b"ne2\x1b[20")  # the end marker is split across frames
    assert [args[0] for args, _ in tmux.calls] == ["send-keys"]
    await client.write(b"1~\x1b[B")
    (keys, _), (load, payload), (paste, _), (arrow, _) = tmux.calls
    assert keys == ("send-keys", "-t", "$7", "-H", "61", "62")
    assert load[:3] == ("load-buffer", "-b", load[2]) and load[2].startswith("aq-input-")
    assert load[3:] == ("-",) and payload == b"line1\nline2"
    assert paste == ("paste-buffer", "-p", "-d", "-b", load[2], "-t", "$7")
    assert arrow == ("send-keys", "-t", "$7", "Down")
    tmux.calls.clear()
    await client.write(b"\x1b[200~\x1b[201~")
    assert tmux.calls == []  # an empty paste sends nothing
    await client.write(b"\x1b[200~one\x1b[201~\x1b[200~two\x1b[201~")
    buffers = [args[2] for args, _ in tmux.calls if args[0] == "load-buffer"]
    assert len(set(buffers)) == 2


async def test_input_client_refuses_oversized_paste_with_4400_without_echo():
    from src.sessions.terminal_input import TerminalInputError
    tmux, client = await _input_client()
    await client.write(b"\x1b[200~" + b"s" * 60000)
    with pytest.raises(TerminalInputError) as caught:
        await client.write(b"s" * 6000 + b"\x1b[201~")
    assert caught.value.code == 4400 and str(caught.value) == "Terminal paste is too large."
    assert tmux.calls == []
    with pytest.raises(TerminalInputError):
        await client.write(b"x" * (64 * 1024 + 1))


async def test_input_client_fences_every_write_and_hides_tmux_diagnostics():
    from src.sessions.terminal_pty import TerminalAttachError
    tmux, client = await _input_client()
    tmux.fail = "send-keys"
    with pytest.raises(TerminalAttachError) as caught:
        await client.write(b"secret")
    assert str(caught.value) == "Terminal connection failed."
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    tmux.fail, tmux.calls = "paste-buffer", []
    with pytest.raises(TerminalAttachError, match="^Terminal connection failed.$"):
        await client.write(b"\x1b[200~secret\x1b[201~")
    load, _, delete = (args for args, _ in tmux.calls)
    assert delete == ("delete-buffer", "-b", load[2])  # a failed paste never leaks a buffer
    tmux.fail, tmux.calls = None, []
    tmux.token = "successor"
    assert not await client.verify()
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await client.write(b"x")
    tmux.token, tmux.session_id = "instance-a", "$8"  # same name and token, new session
    assert not await client.verify()
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await client.write(b"x")
    assert tmux.calls == []


async def test_input_client_attach_refuses_unfenced_sessions_and_close_ends_reads():
    from src.sessions.terminal_input import TerminalInputError, TmuxInputClient
    from src.sessions.terminal_pty import TerminalAttachError
    tmux = FakeTmux()
    tmux.token = "successor"
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await TmuxInputClient.attach(tmux, SimpleNamespace(name="n", instance_token="instance-a"))
    tmux, client = await _input_client()
    assert await client.verify()
    with pytest.raises(TerminalInputError) as caught:
        await client.resize(45, 30)
    assert caught.value.code == 4400
    await client.write(b"\x1b[200~partial")
    pending = asyncio.create_task(client.read(16384))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await client.close()
    await client.close()
    assert await asyncio.wait_for(pending, 1) == b""
    assert await client.read(16384) == b""
    assert not await client.verify()
    with pytest.raises(TerminalAttachError, match="^Terminal session is unavailable.$"):
        await client.write(b"\x1b[201~")
    assert tmux.calls == []  # the partial paste was dropped, never pasted
