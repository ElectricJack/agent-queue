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
    config = SimpleNamespace(api_auth=SimpleNamespace(require_session_token=False, trusted_dashboard_origins=[]))
    store = SimpleNamespace(value=RequestScope(kind="session", session_id="admin", elevated=True), calls=0)
    async def validate(token, **kwargs):
        store.calls += 1
        return store.value if token == "aqs_valid" else None
    store.validate = validate
    orch = SimpleNamespace(db=db, session_providers=SimpleNamespace(create=lambda name: object()))
    return SimpleNamespace(db=db, client=client, attach=attach, config=config, store=store, orch=orch)


def service(setup, **kwargs):
    return module().TerminalStreamService(
        setup.orch, setup.config, token_store=setup.store, attach=setup.attach,
        recheck_seconds=0.02, **kwargs,
    )


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


async def test_plan_approval_live_task_terminal_remains_interactive(setup):
    from src.models import TaskStatus
    task_row = SimpleNamespace(id="t", project_id="p", assigned_agent_id="a", status=TaskStatus.AWAITING_PLAN_APPROVAL, claim_epoch=1)
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
    db = Database(str(tmp_path / "activity.db"))
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
