"""The one dashboard origin that links sent off this machine may name.

Discord posts (escalations, the hourly digest, document reviews) and the
digest's dry-run preview all link into the dashboard SPA.  The SPA is served
by the dashboard server (``dashboard.server``, default ``127.0.0.1:8082``),
never by the daemon, whose API port is loopback-bound and has no pages.  So
the only origin a link may name is one configured for the dashboard itself
(vault spec ``2026-09-24-tailscale-dashboard-link.md`` §4):

1. ``dashboard.server.public_url`` (alias ``dashboard.public_url``) when it is
   a valid, non-loopback http(s) origin: used exactly, with no CLI involved;
2. otherwise the dashboard server's own bind address, when that address is
   this machine's Tailscale address (the edge answers for its bind host);
3. otherwise nothing, with an explicit notice naming the configuration gap.

``health_check.base_url`` is never read.  That fallback named the daemon's
port and was the bug.  A loopback bind is never rewritten to a tailnet
address: a Tailscale identity proves the host is on a tailnet, not that a
loopback-bound port is reachable from a phone.  A resolved URL means
"configured", never "verified reachable"; diagnostics report the two apart.

:class:`DashboardLinkResolver` is the daemon's cached, async front door.
:func:`dashboard_link_base` is the pure decision underneath it, shared with
``aq doctor --check dashboard.remote_link`` and ``aq dashboard link``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PUBLIC_URL_KEY = "dashboard.server.public_url"
HOST_KEY = "dashboard.server.host"

#: ``DashboardLink.reason`` values.  The first two carry a URL.
REASON_PUBLIC_URL = "public_url"
REASON_TAILNET_BIND = "tailnet_bind"
REASON_SERVER_DISABLED = "server_disabled"
REASON_PUBLIC_URL_CONFLICT = "public_url_conflict"
REASON_PUBLIC_URL_INVALID = "public_url_invalid"
REASON_LOCAL_BIND = "local_bind"
REASON_BIND_NOT_TAILNET = "bind_not_tailnet"
REASON_TAILSCALE_UNAVAILABLE = "tailscale_unavailable"
REASON_TAILNET_MISMATCH = "tailnet_address_mismatch"
REASON_RESOLUTION_FAILED = "resolution_failed"

POSITIVE_TTL_SECONDS = 300.0
NEGATIVE_TTL_SECONDS = 30.0
#: Combined deadline for one Tailscale CLI probe, spawn to exit.
PROBE_DEADLINE_SECONDS = 2.0
_PROBE_OUTPUT_LIMIT = 4096

_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_UNAVAILABLE = "Remote dashboard link unavailable ({reason}; open it on the daemon host)."


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkSettings:
    """The allowlisted, non-secret settings a resolution reads -- never the whole config.

    Equality is the cache key: any change bumps the resolver's generation.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8082
    public_url: str = ""
    tailscale_path: str = ""
    #: ``api_auth.trusted_dashboard_origins``; read for diagnostics only.
    trusted_origins: tuple[str, ...] = ()
    #: Non-empty when ``dashboard.public_url`` and ``dashboard.server.public_url``
    #: disagree.  ``load_config`` refuses such a file, so only a caller that
    #: reads the YAML itself (``aq dashboard link``) ever sees it set.
    public_url_conflict: str = ""

    @classmethod
    def from_config(cls, config: Any) -> LinkSettings:
        """Read from an :class:`~src.config.AppConfig` (or anything shaped like one)."""
        server = getattr(config, "dashboard_server", None)
        auth = getattr(config, "api_auth", None)
        origins = getattr(auth, "trusted_dashboard_origins", None) or ()
        return cls(
            enabled=bool(getattr(server, "enabled", False)),
            host=str(getattr(server, "host", "") or ""),
            port=int(getattr(server, "port", 0) or 0),
            public_url=str(getattr(server, "public_url", "") or ""),
            tailscale_path=str(getattr(server, "tailscale_path", "") or ""),
            trusted_origins=tuple(str(origin) for origin in origins),
        )

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> LinkSettings:
        """Read from a parsed ``config.yaml`` through the daemon's own parsers."""
        from src.config import dashboard_public_url_conflict, dashboard_server_config_from_raw

        server = dashboard_server_config_from_raw(raw)
        auth = raw.get("api_auth")
        origins = auth.get("trusted_dashboard_origins") if isinstance(auth, Mapping) else None
        return cls(
            enabled=bool(server.enabled),
            host=str(server.host or ""),
            port=int(server.port or 0),
            public_url=str(server.public_url or ""),
            tailscale_path=str(server.tailscale_path or ""),
            trusted_origins=tuple(str(o) for o in origins) if isinstance(origins, list) else (),
            public_url_conflict=dashboard_public_url_conflict(raw) or "",
        )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardLink:
    """A dashboard origin for external links, or why there is none.

    ``url`` is an origin with no trailing slash (``https://host[:port]``), or
    empty.  ``detail`` is static operator-facing text that never echoes a
    configured value, so it is safe to post verbatim.
    """

    url: str = ""
    reason: str = ""
    detail: str = ""
    source: str = ""
    checked_at: float = 0.0
    generation: int = 0

    @property
    def unavailable_notice(self) -> str:
        """The text a post carries instead of a link; empty when there is a link."""
        if self.url:
            return ""
        return _UNAVAILABLE.format(reason=self.detail or "no dashboard origin is configured")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url or None,
            "reason": self.reason,
            "detail": self.detail,
            "source": self.source or None,
            "checked_at": self.checked_at,
            "generation": self.generation,
            "unavailable_notice": self.unavailable_notice or None,
            # A configured origin is not a tested one (spec §4).
            "remote_reachability": "unverified",
        }


def _unavailable(reason: str, detail: str) -> DashboardLink:
    return DashboardLink(reason=reason, detail=detail)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_local_host(host: str) -> bool:
    """Loopback or wildcard: an address that means "this machine" to whoever reads it."""
    host = host.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    address = _ip(host)
    return address is not None and (address.is_loopback or address.is_unspecified)


def is_tailnet_address(host: str) -> bool:
    address = _ip(host.strip())
    if address is None:
        return False
    return address in (_TAILSCALE_V4 if address.version == 4 else _TAILSCALE_V6)


def _origin(scheme: str, host: str, port: int | None) -> str:
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return f"{scheme}://{authority}"


def normalise_public_origin(value: str) -> tuple[str, str]:
    """``(origin, "")`` for a usable public dashboard origin, else ``("", why)``.

    ``why`` completes the sentence "dashboard.server.public_url ..." and never
    quotes the value, which may carry credentials.
    """
    text = value.strip()
    if not text:
        return "", "is empty"
    if any(ord(char) <= 32 or ord(char) == 127 for char in text) or any(
        char in text for char in "\\*%"
    ):
        return "", "contains whitespace, control characters, wildcards or escapes"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return "", "is not a valid URL"
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return "", "must be an http(s) URL with a host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "", "must not contain credentials"
    if parts.query or parts.fragment or "?" in text or "#" in text:
        return "", "must not contain a query or fragment"
    if parts.path not in {"", "/"}:
        return "", "must be an origin without a path (the dashboard is served at /)"
    host = parts.hostname.lower()
    if is_local_host(host):
        return "", "names a loopback or wildcard address, which means the reader's own machine"
    return _origin(scheme, host, port), ""


@dataclass(frozen=True, slots=True)
class TailnetProbe:
    """What the Tailscale CLI said this machine's tailnet addresses are."""

    addresses: tuple[str, ...] = ()
    #: Why the CLI gave no answer; empty when it did.
    error: str = ""


def needs_tailnet_probe(settings: LinkSettings) -> bool:
    """True only for rule 2: no public URL, and a bind address in a Tailscale range."""
    return (
        settings.enabled
        and not settings.public_url_conflict
        and not settings.public_url.strip()
        and is_tailnet_address(settings.host)
    )


def dashboard_link_base(
    settings: LinkSettings | Any, *, tailnet: TailnetProbe | None = None
) -> DashboardLink:
    """Decide the dashboard origin for external links (spec §4, rules 1-3).

    Pure: the one input that needs the Tailscale CLI -- whether a tailnet
    bind address is this machine's -- arrives as ``tailnet``, and only
    :func:`needs_tailnet_probe` settings ever consult it.
    """
    if not isinstance(settings, LinkSettings):
        settings = LinkSettings.from_config(settings)
    if settings.public_url_conflict:
        return _unavailable(
            REASON_PUBLIC_URL_CONFLICT,
            "dashboard.public_url and dashboard.server.public_url disagree",
        )
    if not settings.enabled:
        return _unavailable(REASON_SERVER_DISABLED, "the dashboard server is disabled")
    if settings.public_url.strip():
        origin, why = normalise_public_origin(settings.public_url)
        if not origin:
            return _unavailable(REASON_PUBLIC_URL_INVALID, f"{PUBLIC_URL_KEY} {why}")
        return DashboardLink(url=origin, reason=REASON_PUBLIC_URL, source=PUBLIC_URL_KEY)

    host = settings.host.strip().lower()
    if is_local_host(host):
        return _unavailable(
            REASON_LOCAL_BIND,
            f"{PUBLIC_URL_KEY} is not set and the dashboard server listens on a loopback or "
            "wildcard address",
        )
    if not is_tailnet_address(host):
        return _unavailable(
            REASON_BIND_NOT_TAILNET,
            f"{PUBLIC_URL_KEY} is not set and {HOST_KEY} is not a Tailscale address",
        )
    if tailnet is None or tailnet.error:
        return _unavailable(
            REASON_TAILSCALE_UNAVAILABLE,
            f"{PUBLIC_URL_KEY} is not set and the Tailscale CLI could not confirm {HOST_KEY}",
        )
    bind = _ip(host)
    if bind is None or all(_ip(address) != bind for address in tailnet.addresses):
        return _unavailable(
            REASON_TAILNET_MISMATCH,
            f"{PUBLIC_URL_KEY} is not set and {HOST_KEY} is not this machine's Tailscale address",
        )
    return DashboardLink(
        url=_origin("http", str(bind), settings.port),
        reason=REASON_TAILNET_BIND,
        source=HOST_KEY,
    )


def local_dashboard_url(settings: LinkSettings | Any) -> str | None:
    """Where a browser on this machine reaches the dashboard server, or ``None`` if disabled.

    The local mode: a wildcard bind renders as ``127.0.0.1``, and the result
    ends in ``/``.  The daemon's ``/dashboard`` pointer uses it; it is never
    a link for anyone else.
    """
    if not isinstance(settings, LinkSettings):
        settings = LinkSettings.from_config(settings)
    if not settings.enabled:
        return None
    host = settings.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}/"


def edge_compatibility(link: DashboardLink, settings: LinkSettings) -> dict[str, Any]:
    """Would the dashboard server's edge (Host and Origin gates) answer for ``link.url``?

    Mirrors ``src/dashboard_server/edge.py`` without importing it (the
    daemon never loads the dashboard server): the Host gate admits the bind
    address and the hostnames of trusted origins; the Origin gate admits a
    trusted origin, or a same-origin request, which only a direct http bind
    produces.  A proxy's own header rewriting is out of view:
    ``remote_reachability`` stays ``unverified`` either way.
    """
    if not link.url:
        return {"host_allowed": None, "origin_allowed": None, "trusted": False}
    parts = urlsplit(link.url)
    hostname = (parts.hostname or "").lower()
    trusted: set[str] = set()
    trusted_hosts: set[str] = set()
    for value in settings.trusted_origins:
        origin, _why = normalise_public_origin(value)
        if origin:
            trusted.add(origin)
            trusted_hosts.add((urlsplit(origin).hostname or "").lower())
    bind = settings.host.strip().lower()
    is_bind = bind not in {"0.0.0.0", "::"} and _ip(hostname) is not None and (
        _ip(hostname) == _ip(bind)
    )
    is_trusted = link.url in trusted
    return {
        "host_allowed": is_bind or hostname in trusted_hosts,
        "origin_allowed": is_trusted or (is_bind and parts.scheme == "http"),
        "trusted": is_trusted,
    }


def describe_link(
    settings: LinkSettings,
    link: DashboardLink,
    *,
    server_health: str,
    discord_channel_configured: bool,
) -> dict[str, Any]:
    """The diagnostic report ``aq doctor`` and ``aq dashboard link`` share.

    Only allowlisted, non-secret facts: the chosen origin and why, the edge's
    verdict on it, the server's state, and whether a Tailscale CLI is there
    to run.  Never the process environment, never the rest of the config.
    """
    executable, why = tailscale_executable(settings)
    return {
        **link.to_dict(),
        "edge": edge_compatibility(link, settings),
        "server": {
            "enabled": settings.enabled,
            "local_url": local_dashboard_url(settings),
            "health": server_health,
        },
        "tailscale_cli": {
            "configured": bool(settings.tailscale_path.strip()),
            "path": executable,
            "problem": why or None,
            # Consulted only when dashboard.server.host is a tailnet address.
            "consulted": needs_tailnet_probe(settings),
        },
        "discord_channel_configured": discord_channel_configured,
    }


# ---------------------------------------------------------------------------
# The Tailscale probe
# ---------------------------------------------------------------------------


def tailscale_executable(settings: LinkSettings) -> tuple[str | None, str]:
    """``(path, "")`` for the CLI to run, else ``(None, why)``.

    Only the configured ``dashboard.server.tailscale_path`` or ``PATH``: no
    speculative list of install locations (spec §4.1).
    """
    configured = settings.tailscale_path.strip()
    if configured:
        path = os.path.expanduser(configured)
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path, ""
        return None, "dashboard.server.tailscale_path is not an executable file"
    found = shutil.which("tailscale")
    if found:
        return found, ""
    return None, "the tailscale CLI is not on the daemon's PATH"


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    data = bytearray()
    while len(data) <= _PROBE_OUTPUT_LIMIT:
        chunk = await stream.read(_PROBE_OUTPUT_LIMIT + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return bytes(data)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Kill the probe's process group and drain its pipe, within a second.

    asyncio's ``Process.wait()`` returns only once every pipe has closed, so
    a child killed mid-write -- or a wrapper script whose own child still
    holds stdout -- would hang a bare ``wait()``.  Killing the whole session
    closes every writer; draining lets the transport see EOF.
    """
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    try:
        await asyncio.wait_for(process.communicate(), timeout=1.0)
    except (TimeoutError, OSError, ValueError):
        logger.debug("tailscale probe cleanup did not finish", exc_info=True)


async def probe_tailnet(
    settings: LinkSettings, *, deadline: float = PROBE_DEADLINE_SECONDS
) -> TailnetProbe:
    """Run ``tailscale ip`` off the event loop's back, bounded in time and output."""
    executable, why = tailscale_executable(settings)
    if executable is None:
        return TailnetProbe(error=why)
    process: asyncio.subprocess.Process | None = None

    async def _run() -> tuple[int, bytes]:
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            executable,
            "ip",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdout is not None
        output = await _read_bounded(process.stdout)
        if len(output) > _PROBE_OUTPUT_LIMIT:
            return -1, output
        return await process.wait(), output

    try:
        code, output = await asyncio.wait_for(_run(), timeout=deadline)
    except TimeoutError:
        return TailnetProbe(error=f"tailscale ip did not answer within {deadline:g}s")
    except OSError as error:
        return TailnetProbe(error=f"tailscale ip could not run ({error.__class__.__name__})")
    finally:
        if process is not None:
            await _reap(process)
    if code == -1:
        return TailnetProbe(error="tailscale ip printed more output than expected")
    if code != 0:
        return TailnetProbe(error=f"tailscale ip exited with status {code}")
    addresses = []
    for line in output.decode("utf-8", "replace").splitlines():
        candidate = line.strip()
        if is_tailnet_address(candidate):
            addresses.append(str(_ip(candidate)))
    if not addresses:
        return TailnetProbe(error="tailscale ip reported no tailnet address")
    return TailnetProbe(addresses=tuple(addresses))


Probe = Callable[[LinkSettings], Awaitable[TailnetProbe]]


async def resolve_dashboard_link(
    settings: LinkSettings, *, probe: Probe = probe_tailnet
) -> DashboardLink:
    """One uncached resolution: probe only when rule 2 needs it, then decide."""
    tailnet = await probe(settings) if needs_tailnet_probe(settings) else None
    return dashboard_link_base(settings, tailnet=tailnet)


# ---------------------------------------------------------------------------
# The daemon's cached resolver
# ---------------------------------------------------------------------------


class DashboardLinkSource(Protocol):
    """What a sender needs: the link to render into a new payload."""

    async def resolve(self) -> DashboardLink: ...


@dataclass
class _Cached:
    settings: LinkSettings
    link: DashboardLink
    expires_at: float


@dataclass
class DashboardLinkResolver:
    """Resolve on demand, cache briefly, and never raise.

    Settings are read through ``config_getter`` on every call, so a
    hot-reloaded ``dashboard.server`` section invalidates the cache at once
    (a new generation); otherwise a link is reused for
    :data:`POSITIVE_TTL_SECONDS` and an absence for
    :data:`NEGATIVE_TTL_SECONDS`, so a Tailscale daemon that comes up after
    AQ is noticed within half a minute.  Each new outcome is logged once.
    """

    config_getter: Callable[[], Any]
    probe: Probe = probe_tailnet
    clock: Callable[[], float] = time.time
    positive_ttl: float = POSITIVE_TTL_SECONDS
    negative_ttl: float = NEGATIVE_TTL_SECONDS
    generation: int = 0
    _cached: _Cached | None = field(default=None, repr=False)
    _last_logged: tuple[str, str, str] | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def settings(self) -> LinkSettings:
        return LinkSettings.from_config(self.config_getter())

    def peek(self) -> DashboardLink | None:
        """The last resolution, however old; ``None`` before the first."""
        return self._cached.link if self._cached is not None else None

    async def resolve(self) -> DashboardLink:
        try:
            settings = self.settings()
        except Exception:
            logger.warning("dashboard link settings could not be read", exc_info=True)
            return _unavailable(REASON_RESOLUTION_FAILED, "dashboard link settings are unreadable")
        try:
            async with self._lock:
                return await self._resolve_locked(settings)
        except Exception:
            logger.warning("dashboard link resolution failed", exc_info=True)
            return _unavailable(REASON_RESOLUTION_FAILED, "dashboard link resolution failed")

    async def _resolve_locked(self, settings: LinkSettings) -> DashboardLink:
        cached = self._cached
        now = self.clock()
        if cached is not None and cached.settings == settings and now < cached.expires_at:
            return cached.link
        if cached is None or cached.settings != settings:
            self.generation += 1
        try:
            link = await resolve_dashboard_link(settings, probe=self.probe)
        except Exception:
            logger.warning("dashboard link resolution failed", exc_info=True)
            link = _unavailable(REASON_RESOLUTION_FAILED, "dashboard link resolution failed")
        now = self.clock()
        link = replace(link, checked_at=now, generation=self.generation)
        ttl = self.positive_ttl if link.url else self.negative_ttl
        self._cached = _Cached(settings=settings, link=link, expires_at=now + ttl)
        self._log(link)
        return link

    def _log(self, link: DashboardLink) -> None:
        outcome = (link.url, link.reason, link.source)
        if outcome == self._last_logged:
            return
        self._last_logged = outcome
        if link.url:
            logger.info(
                "Dashboard links name %s (source %s, reason %s; remote reachability unverified)",
                link.url, link.source, link.reason,
            )
        else:
            logger.warning(
                "Dashboard links unavailable (reason %s): %s", link.reason, link.detail,
            )
