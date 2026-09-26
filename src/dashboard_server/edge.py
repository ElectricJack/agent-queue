"""Host, Origin and peer gates for every proxied request (spec §3.2, §3.3).

The daemon checks ``Origin`` only on ``/ws/terminal``, and behind any proxy it
sees every connection arrive from loopback.  The dashboard server sees the
real peer and the real headers, so it applies these before a request reaches
the daemon:

* **Host gate** -- the ``Host`` hostname must be literal loopback, the
  configured concrete bind address, or the hostname of a trusted origin;
  otherwise ``421``.  This is what stops DNS rebinding against ``/api``.
* **Origin gate** -- an ``Origin``, when present, must equal ``scheme://Host``
  or be trusted; otherwise ``403 origin_not_allowed``.  No ``Origin`` (curl, a
  same-origin ``GET``) passes.
* **Peer gate** -- non-loopback terminal WebSockets require an explicitly
  trusted Origin. Requests carrying ``Authorization`` or an ``aq-bearer.*``
  subprotocol remain loopback-only, even for trusted origins. No forwarding
  header is invented for the daemon to trust instead.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.dashboard_server.settings import DashboardServerSettings, normalise_origin

_WILDCARD_BINDS = frozenset({"0.0.0.0", "::"})
_BEARER_PREFIX = "aq-bearer."
_TERMINAL_PREFIX = "/ws/terminal/"


@dataclass(frozen=True)
class EdgeDenial:
    """Why a proxied request was refused before it reached the daemon."""

    status: int
    error: str
    message: str

    def body(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "message": self.message}


def _headers(scope: dict[str, Any], name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", ())
        if key.lower() == name
    ]


def _split_host(value: str) -> str | None:
    """The hostname of a ``Host`` header value, lower-cased, brackets removed."""
    value = value.strip().lower()
    if not value or any(char in value for char in "/\\@?# \t"):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        rest = value[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return value[1:end]
    host, sep, port = value.rpartition(":")
    if sep and ":" not in host:
        if not port.isdigit():
            return None
        return host or None
    return value


def is_loopback_host(hostname: str) -> bool:
    """``localhost`` or a loopback IP literal (IPv4-mapped included)."""
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _peer_is_loopback(scope: dict[str, Any]) -> bool:
    client = scope.get("client")
    if not client:
        # No peer address (a Unix socket or an in-process test transport):
        # nothing proves it is local, so it is not.
        return False
    host = str(client[0])
    # A peer is an address, never a name: ``localhost`` here proves nothing.
    return host != "localhost" and is_loopback_host(host)


def _hostname_of_origin(origin: str) -> str:
    host = origin.split("://", 1)[1]
    if host.startswith("["):
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if ":" in host else host


class EdgeGate:
    """The three gates, bound to one process's settings."""

    def __init__(self, settings: DashboardServerSettings) -> None:
        self._trusted = frozenset(settings.trusted_origins)
        allowed = {_hostname_of_origin(origin) for origin in self._trusted}
        bind = settings.host.lower()
        if bind not in _WILDCARD_BINDS:
            allowed.add(bind)
        self._allowed_hostnames = frozenset(allowed)

    def host_allowed(self, hostname: str) -> bool:
        return is_loopback_host(hostname) or hostname in self._allowed_hostnames

    def check(self, scope: dict[str, Any]) -> EdgeDenial | None:
        """``None`` when the request may be proxied, else the refusal to send."""
        hosts = _headers(scope, b"host")
        hostname = _split_host(hosts[0]) if len(hosts) == 1 else None
        if hostname is None or not self.host_allowed(hostname):
            return EdgeDenial(
                421,
                "misdirected_host",
                "This dashboard server does not answer for that host name. Use a loopback "
                "address, the configured dashboard.server.host, or add the origin to "
                "api_auth.trusted_dashboard_origins.",
            )

        origins = _headers(scope, b"origin")
        origin = None
        if origins:
            scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
            origin = normalise_origin(origins[0]) if len(origins) == 1 else None
            expected = normalise_origin(f"{scheme}://{hosts[0].strip()}")
            if origin is None or (origin != expected and origin not in self._trusted):
                return EdgeDenial(
                    403,
                    "origin_not_allowed",
                    "This origin may not drive this install. Add it to "
                    "api_auth.trusted_dashboard_origins if it should.",
                )

        if not _peer_is_loopback(scope) and self._needs_loopback(scope, origin):
            return EdgeDenial(
                403,
                "loopback_only",
                "Remote terminal WebSockets require an Origin listed in "
                "api_auth.trusted_dashboard_origins. Bearer-token requests require a local "
                "connection. Use a trusted dashboard origin or forward the port over SSH.",
            )
        return None

    def _needs_loopback(self, scope: dict[str, Any], origin: str | None) -> bool:
        if _headers(scope, b"authorization"):
            return True
        if any(protocol.startswith(_BEARER_PREFIX) for protocol in _subprotocols(scope)):
            return True
        path = scope.get("path", "")
        if path == _TERMINAL_PREFIX.rstrip("/") or path.startswith(_TERMINAL_PREFIX):
            return scope.get("type") != "websocket" or origin not in self._trusted
        return False


def _subprotocols(scope: dict[str, Any]) -> Iterable[str]:
    offered = scope.get("subprotocols")
    if offered:
        yield from (str(protocol).strip() for protocol in offered)
    for header in _headers(scope, b"sec-websocket-protocol"):
        yield from (protocol.strip() for protocol in header.split(","))
