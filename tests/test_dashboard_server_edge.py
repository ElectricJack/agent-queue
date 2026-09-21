"""Host, Origin and peer gates (docs/specs/dashboard-server.md §3.2, §3.3, §7 "Edge gates")."""

from __future__ import annotations

import pytest

from src.dashboard_server.edge import EdgeGate, is_loopback_host
from src.dashboard_server.settings import DashboardServerSettings, settings_from_config

LOOPBACK_PEER = ("127.0.0.1", 50000)
LAN_PEER = ("192.168.1.9", 50000)


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure edge-gate tests never allocate a database."""


def _scope(
    path: str = "/api/health",
    *,
    host: str | None = "127.0.0.1:8082",
    origin: str | None = None,
    client: tuple[str, int] | None = LOOPBACK_PEER,
    kind: str = "http",
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    subprotocols: list[str] | None = None,
) -> dict:
    raw: list[tuple[bytes, bytes]] = []
    if host is not None:
        raw.append((b"host", host.encode()))
    if origin is not None:
        raw.append((b"origin", origin.encode()))
    raw.extend(headers or [])
    scope = {
        "type": kind,
        "path": path,
        "raw_path": path.encode(),
        "scheme": scheme,
        "headers": raw,
        "client": client,
    }
    if subprotocols is not None:
        scope["subprotocols"] = subprotocols
    return scope


def _gate(**settings) -> EdgeGate:
    return EdgeGate(DashboardServerSettings(**settings))


def _error(gate: EdgeGate, scope: dict) -> str | None:
    denial = gate.check(scope)
    return None if denial is None else f"{denial.status} {denial.error}"


# -- Host gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8082", "127.0.0.1", "localhost:8082", "LOCALHOST:8082", "[::1]:8082", "[::1]"],
)
def test_loopback_hosts_are_admitted_by_default(host):
    assert _error(_gate(), _scope(host=host)) is None


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8082",
        "127.0.0.1.nip.io:8082",
        "192.168.1.5:8082",
        "",
        "127.0.0.1:8082@evil.example",
        "[::1",
        "127.0.0.1:port",
    ],
)
def test_a_foreign_or_malformed_host_is_misdirected(host):
    """DNS rebinding: a name that resolves to loopback is still not loopback."""
    assert _error(_gate(), _scope(host=host)) == "421 misdirected_host"


def test_a_missing_or_repeated_host_is_misdirected():
    gate = _gate()
    assert _error(gate, _scope(host=None)) == "421 misdirected_host"
    repeated = _scope(headers=[(b"host", b"127.0.0.1:8082")])
    assert _error(gate, repeated) == "421 misdirected_host"


def test_a_concrete_bind_address_is_an_allowed_host_but_a_wildcard_is_not():
    lan = _gate(host="192.168.1.5")
    assert _error(lan, _scope(host="192.168.1.5:8082", client=LAN_PEER)) is None
    wildcard = _gate(host="0.0.0.0")
    assert _error(wildcard, _scope(host="0.0.0.0:8082")) == "421 misdirected_host"
    assert _error(wildcard, _scope(host="192.168.1.5:8082")) == "421 misdirected_host"


def test_a_trusted_origins_hostname_is_an_allowed_host():
    settings = settings_from_config(
        {"api_auth": {"trusted_dashboard_origins": ["https://AQ.example.com"]}}, environ={},
    )
    gate = EdgeGate(settings)
    assert _error(gate, _scope(host="aq.example.com", scheme="https")) is None
    assert _error(gate, _scope(host="other.example.com")) == "421 misdirected_host"


# -- Origin gate -------------------------------------------------------------


def test_no_origin_passes():
    """curl and same-origin GETs carry no Origin."""
    assert _error(_gate(), _scope(origin=None)) is None


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8082", "http://127.0.0.1:8082"),
        ("localhost:8082", "http://localhost:8082"),
        ("[::1]:8082", "http://[::1]:8082"),
        ("127.0.0.1", "http://127.0.0.1:80"),
    ],
)
def test_a_same_origin_request_passes(host, origin):
    assert _error(_gate(), _scope(host=host, origin=origin)) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "http://localhost:8082",  # a different origin from Host 127.0.0.1:8082
        "https://127.0.0.1:8082",
        "http://127.0.0.1:5173",
        "null",
        "http://127.0.0.1:8082/path",
    ],
)
def test_a_cross_origin_request_is_refused(origin):
    assert _error(_gate(), _scope(origin=origin)) == "403 origin_not_allowed"


def test_a_repeated_origin_is_refused():
    scope = _scope(origin="http://127.0.0.1:8082", headers=[(b"origin", b"http://127.0.0.1:8082")])
    assert _error(_gate(), scope) == "403 origin_not_allowed"


def test_a_trusted_origin_is_admitted_behind_a_tls_front_proxy():
    """The browser says https://aq.example.com; the front proxy speaks http to us."""
    settings = settings_from_config(
        {"api_auth": {"trusted_dashboard_origins": ["https://aq.example.com"]}}, environ={},
    )
    scope = _scope(host="aq.example.com", origin="https://aq.example.com:443")
    assert _error(EdgeGate(settings), scope) is None


def test_the_origin_gate_applies_to_websocket_handshakes():
    scope = _scope("/ws/events", kind="websocket", origin="http://evil.example")
    assert _error(_gate(), scope) == "403 origin_not_allowed"
    same = _scope("/ws/events", kind="websocket", origin="http://127.0.0.1:8082")
    assert _error(_gate(), same) is None


# -- Peer gate ---------------------------------------------------------------


def _lan_gate() -> EdgeGate:
    return _gate(host="192.168.1.5")


def _lan_scope(path: str = "/api/health", **kwargs) -> dict:
    return _scope(path, host="192.168.1.5:8082", client=LAN_PEER, **kwargs)


def test_a_lan_peer_reaches_local_operator_routes():
    """§3.4: the API itself is reachable from a LAN bind -- that is the exposure."""
    assert _error(_lan_gate(), _lan_scope()) is None
    assert _error(_lan_gate(), _lan_scope("/ws/events", kind="websocket")) is None


@pytest.mark.parametrize("kind", ["websocket", "http"])
def test_a_lan_peer_is_refused_terminals(kind):
    for path in ("/ws/terminal/sess-1", "/ws/terminal"):
        assert _error(_lan_gate(), _lan_scope(path, kind=kind)) == "403 loopback_only"


def test_a_lan_peer_is_refused_bearer_tokens():
    authorized = _lan_scope(headers=[(b"authorization", b"Bearer aqs_x")])
    assert _error(_lan_gate(), authorized) == "403 loopback_only"
    bearer = _lan_scope(
        "/ws/events", kind="websocket", subprotocols=["aq-terminal-v1", "aq-bearer.aqs_x"],
    )
    assert _error(_lan_gate(), bearer) == "403 loopback_only"
    header_only = _lan_scope(headers=[(b"sec-websocket-protocol", b"aq-terminal-v1, aq-bearer.x")])
    assert _error(_lan_gate(), header_only) == "403 loopback_only"


def test_a_loopback_peer_keeps_terminals_and_bearer_tokens():
    gate = _gate()
    assert _error(gate, _scope("/ws/terminal/s", kind="websocket")) is None
    assert _error(gate, _scope(headers=[(b"authorization", b"Bearer aqs_x")])) is None
    mapped = _scope("/ws/terminal/s", kind="websocket", client=("::ffff:127.0.0.1", 1))
    assert _error(gate, mapped) is None
    assert _error(gate, _scope("/ws/terminal/s", kind="websocket", client=("::1", 1))) is None


def test_an_unknown_peer_is_not_loopback():
    """No address (a Unix socket, an in-process transport) proves nothing."""
    gate = _gate()
    assert _error(gate, _scope("/ws/terminal/s", client=None)) == "403 loopback_only"
    assert _error(gate, _scope("/ws/terminal/s", client=("localhost", 1))) == "403 loopback_only"


def test_the_gates_run_in_order_host_then_origin_then_peer():
    scope = _lan_scope("/ws/terminal/s", origin="http://evil.example")
    assert _error(_gate(), scope) == "421 misdirected_host"
    assert _error(_lan_gate(), scope) == "403 origin_not_allowed"


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.5")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("10.0.0.1")
    assert not is_loopback_host("localhost.evil.example")
