"""Operator host shells: config gate, operator and remote-viewer auth, lifecycle, env scrub."""
import shlex
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import RequestScope
from src.api.terminal_stream import TerminalStreamError, build_terminal_router
from src.config import HostShellConfig, load_config
from src.sessions import host_shell as hs
from src.sessions.provider import SessionError


class FakeTmux:
    """Just enough of a tmux server for HostShellManager."""

    def __init__(self):
        self.sessions = {}  # name -> {"env": {}, "opts": {}, "cmd": str, "created": int}
        self.globals = {"AQ_SESSION_TOKEN": "secret", "AGENT_QUEUE_DB": "pg://x", "HOME": "/h"}
        self.removed = []

    async def _tmux(self, *args, timeout=None, stdin=None):
        cmd = args[0]
        if cmd == "list-sessions":
            return "".join(f"{n}\t{s['created']}\t0\n" for n, s in self.sessions.items())
        if cmd == "new-session":
            name = args[args.index("-s") + 1]
            self.sessions[name] = {"env": {}, "opts": {}, "cmd": args[-1], "created": 100}
            return ""
        if cmd == "show-environment" and "-g" in args:
            return "".join(f"{k}={v}\n" for k, v in self.globals.items())
        raw = args[args.index("-t") + 1]
        if cmd in ("set-option", "show-options") and raw.startswith("=") and not raw.endswith(":"):
            # tmux 3.4 reads a bare "=name" as a pane target for options.
            raise SessionError(f"no such session: {raw}")
        target = raw.lstrip("=").removesuffix(":")
        target = target.removeprefix("$")
        session = self.sessions.get(target)
        if session is None:
            raise SessionError("can't find session")
        if cmd == "set-environment" and "-r" in args:
            self.removed.append((target, args[-1]))
        elif cmd == "set-environment":
            session["env"][args[-2]] = args[-1]
        elif cmd == "show-environment":
            key = args[-1]
            return f"{key}={session['env'][key]}\n" if key in session["env"] else f"-{key}\n"
        elif cmd == "set-option":
            session["opts"][args[-2]] = args[-1]
        elif cmd == "show-options":
            return session["opts"].get(args[-1], "") + "\n"
        elif cmd == "kill-session":
            del self.sessions[target]
        return ""


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()

    async def resolve(provider, name, token):
        session = provider.sessions.get(name)
        if session is None or session["env"].get("AQ_INSTANCE_TOKEN") != token:
            return None
        return "$" + name

    monkeypatch.setattr(hs, "resolve_session_id", resolve)
    return fake


async def test_open_reattach_close_lifecycle(tmux):
    manager = hs.HostShellManager(tmux)
    first = await manager.open(max_shells=2)
    second = await manager.open(max_shells=2)
    assert (first.name, second.name) == ("aq-host-shell-1", "aq-host-shell-2")
    with pytest.raises(hs.HostShellError):
        await manager.open(max_shells=2)
    # Reattach: a fresh manager (a page reload) finds the same shell and token.
    again = await hs.HostShellManager(tmux).get(first.name)
    assert again.instance_token == first.instance_token
    assert await manager.close(first.name) is True
    assert await manager.get(first.name) is None
    assert await manager.close(first.name) is False
    # The freed number is reused.
    assert (await manager.open(max_shells=2)).name == "aq-host-shell-1"


async def test_unmarked_or_agent_sessions_are_not_host_shells(tmux):
    tmux.sessions["aq-host-shell-9"] = {"env": {"AQ_INSTANCE_TOKEN": "t"}, "opts": {}, "cmd": "", "created": 1}
    tmux.sessions["s-agent"] = {"env": {"AQ_INSTANCE_TOKEN": "t"}, "opts": {hs._MARKER_OPTION: "1"},
                                "cmd": "", "created": 1}
    assert await hs.HostShellManager(tmux).list() == []
    assert not hs.is_host_shell_name("s-agent")
    assert not hs.is_host_shell_name("aq-host-shell-0")


async def test_shell_environment_scrubs_aq_tokens(tmux, monkeypatch):
    monkeypatch.setenv("AQ_SESSION_TOKEN", "aqs_secret")
    monkeypatch.setenv("AQ_INSTANCE_TOKEN", "inst")
    monkeypatch.setenv("AGENT_QUEUE_DB_URL", "postgresql://secret")
    shell = await hs.HostShellManager(tmux).open(max_shells=1)
    argv = shlex.split(tmux.sessions[shell.name]["cmd"])
    assert argv[:2] == ["env", "-i"] and argv[-1] == "-l"
    assert not [a for a in argv if a.startswith(("AQ_", "AGENT_QUEUE"))]
    assert "secret" not in tmux.sessions[shell.name]["cmd"]
    removed = {key for name, key in tmux.removed if name == shell.name}
    assert {"AQ_SESSION_TOKEN", "AGENT_QUEUE_DB"} <= removed and "HOME" not in removed


def _app(tmux, *, enabled=True, store=None, allow_remote=False):
    events = []

    async def log_event(event_type, **kwargs):
        events.append((event_type, kwargs))

    config = SimpleNamespace(
        api_auth=SimpleNamespace(require_session_token=False, trusted_dashboard_origins=[]),
        host_shell=HostShellConfig(enabled=enabled, max_shells=2, allow_remote=allow_remote),
    )
    orch = SimpleNamespace(db=SimpleNamespace(log_event=log_event),
                           session_providers=SimpleNamespace(create=lambda name: tmux))
    app = FastAPI()
    app.include_router(build_terminal_router(orch, config, token_store=store))
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)), events


def test_api_open_list_close_is_audited(tmux):
    client, events = _app(tmux)
    opened = client.post("/api/host-shell")
    assert opened.status_code == 200, opened.text
    name = opened.json()["shell"]["name"]
    listed = client.get("/api/host-shell").json()
    assert listed["enabled"] and [s["name"] for s in listed["shells"]] == [name]
    assert "instance_token" not in str(listed)
    assert client.post(f"/api/host-shell/{name}/close").status_code == 200
    assert [e for e, _ in events] == ["host_shell.opened", "host_shell.closed"]
    assert "local-operator" in events[0][1]["payload"]


def test_api_refuses_when_disabled(tmux):
    client, events = _app(tmux, enabled=False)
    assert client.get("/api/host-shell").json() == {"enabled": False, "shells": []}
    assert client.post("/api/host-shell").status_code == 403
    assert client.post("/api/host-shell/aq-host-shell-1/close").status_code == 403
    assert tmux.sessions == {} and events == []


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer aqs_worker"},          # worker or supervisor token
    {"x-aq-dashboard-viewer": "other"},              # dashboard edge: not the operator
    {"host": "evil.example"},                         # DNS-rebound loopback
])
def test_api_refuses_non_operators(tmux, headers):
    client, _ = _app(tmux)
    for method, path in [("get", "/api/host-shell"), ("post", "/api/host-shell")]:
        assert getattr(client, method)(path, headers=headers).status_code == 403
    assert tmux.sessions == {}


def test_api_refuses_remote_peer(tmux):
    client, _ = _app(tmux)
    remote = TestClient(client.app, base_url="http://localhost", client=("10.0.0.5", 5000))
    assert remote.post("/api/host-shell").status_code == 403


# What the dashboard edge stamps on a browser it proxied from another machine.
REMOTE_VIEWER = {"x-aq-dashboard-viewer": "other", "x-aq-dashboard-peer": "192.168.1.9"}


def test_api_remote_dashboard_viewer_needs_allow_remote(tmux):
    client, events = _app(tmux, allow_remote=False)
    assert client.get("/api/host-shell", headers=REMOTE_VIEWER).status_code == 403
    refused = client.post("/api/host-shell", headers=REMOTE_VIEWER)
    assert refused.status_code == 403 and "allow_remote" in refused.json()["detail"]
    assert "local operator or an allowed remote dashboard viewer" in refused.json()["detail"]
    assert tmux.sessions == {} and events == []

    client, events = _app(tmux, allow_remote=True)
    opened = client.post("/api/host-shell", headers=REMOTE_VIEWER)
    assert opened.status_code == 200, opened.text
    name = opened.json()["shell"]["name"]
    listed = client.get("/api/host-shell", headers=REMOTE_VIEWER).json()
    assert [s["name"] for s in listed["shells"]] == [name]
    assert client.post(f"/api/host-shell/{name}/close", headers=REMOTE_VIEWER).status_code == 200
    assert [e for e, _ in events] == ["host_shell.opened", "host_shell.closed"]
    assert all("remote-dashboard-viewer (peer 192.168.1.9)" in kw["payload"] for _, kw in events)


@pytest.mark.parametrize("allow_remote", [False, True])
@pytest.mark.parametrize("viewer", [None, "operator", "other"])
def test_api_always_refuses_bearer_tokens(tmux, allow_remote, viewer):
    client, events = _app(tmux, allow_remote=allow_remote)
    headers = {"Authorization": "Bearer aqs_supervisor"}
    if viewer:
        headers["x-aq-dashboard-viewer"] = viewer
    for method, path in [("get", "/api/host-shell"), ("post", "/api/host-shell")]:
        refused = getattr(client, method)(path, headers=headers)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Host shells do not accept bearer tokens"
    assert tmux.sessions == {} and events == []


def test_allow_remote_admits_only_the_edges_verdict(tmux):
    """A peer reaching the daemon directly is no dashboard viewer, whatever it claims."""
    client, _ = _app(tmux, allow_remote=True)
    direct = TestClient(client.app, base_url="http://localhost", client=("10.0.0.5", 5000))
    assert direct.post("/api/host-shell", headers=REMOTE_VIEWER).status_code == 403
    rebound = client.post("/api/host-shell", headers={"host": "evil.example"})
    assert rebound.status_code == 403
    assert tmux.sessions == {}


@pytest.mark.parametrize("token", ["aqs_supervisor", None])
async def test_terminal_attach_refuses_tokens_and_disabled(tmux, token):
    from src.api.terminal_stream import TerminalStreamService

    store = SimpleNamespace(validate=None)

    async def validate(value, **kwargs):
        return RequestScope(kind="session", session_id="sup", elevated=True)

    store.validate = validate
    ws = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"host": "localhost"},
                         url=SimpleNamespace(hostname="localhost"))
    config = SimpleNamespace(api_auth=SimpleNamespace(require_session_token=False),
                             host_shell=HostShellConfig(enabled=token is not None))
    service = TerminalStreamService(SimpleNamespace(), config, token_store=store)
    with pytest.raises(TerminalStreamError) as err:
        await service._authorize(ws, token, host_shell=True)
    assert err.value.code == 4403


def _attach_service(*, allow_remote, enabled=True):
    from src.api.terminal_stream import TerminalStreamService

    async def validate(value, **kwargs):
        return RequestScope(kind="session", session_id="sup", elevated=True)

    config = SimpleNamespace(
        api_auth=SimpleNamespace(require_session_token=False),
        host_shell=HostShellConfig(enabled=enabled, allow_remote=allow_remote),
    )
    return TerminalStreamService(SimpleNamespace(), config,
                                 token_store=SimpleNamespace(validate=validate))


def _edge_ws(viewer):
    """A WebSocket the dashboard edge relayed: loopback peer, the edge's verdict."""
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                           headers={"host": "192.168.1.69:5173", "x-aq-dashboard-viewer": viewer,
                                    "x-aq-dashboard-peer": "192.168.1.9"},
                           url=SimpleNamespace(hostname="192.168.1.69"))


async def test_terminal_attach_admits_remote_viewer_only_with_allow_remote():
    with pytest.raises(TerminalStreamError) as err:
        await _attach_service(allow_remote=False)._authorize(
            _edge_ws("other"), None, host_shell=True)
    assert err.value.code == 4403
    assert "local operator or an allowed remote dashboard viewer" in str(err.value)
    await _attach_service(allow_remote=True)._authorize(_edge_ws("other"), None, host_shell=True)
    await _attach_service(allow_remote=False)._authorize(
        _edge_ws("operator"), None, host_shell=True)


@pytest.mark.parametrize("viewer", ["operator", "other"])
async def test_terminal_attach_refuses_tokens_even_with_allow_remote(viewer):
    with pytest.raises(TerminalStreamError) as err:
        await _attach_service(allow_remote=True)._authorize(
            _edge_ws(viewer), "aqs_supervisor", host_shell=True)
    assert err.value.code == 4403
    assert str(err.value) == "Host shells do not accept bearer tokens"


async def test_allow_remote_does_not_open_disabled_host_shells():
    with pytest.raises(TerminalStreamError) as err:
        await _attach_service(allow_remote=True, enabled=False)._authorize(
            _edge_ws("other"), None, host_shell=True)
    assert err.value.code == 4403


def test_allow_remote_loads_from_yaml_and_must_be_a_bool(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
        "dashboard": {"host_shell": {"enabled": True, "allow_remote": True}},
    }))
    assert load_config(str(path)).host_shell.allow_remote is True
    assert HostShellConfig().allow_remote is False
    errors = HostShellConfig(allow_remote="yes").validate()
    assert [(e.section, e.field) for e in errors] == [("dashboard.host_shell", "allow_remote")]
