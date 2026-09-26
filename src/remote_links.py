"""The one dashboard origin that links sent off this machine may name.

Discord digests, escalations, document reviews and the digest preview all
render their dashboard links from :class:`DashboardLinkResolver`
(docs: tailscale-dashboard-link spec §4).  The rules:

1. A valid ``dashboard.server.public_url`` (alias ``dashboard.public_url``)
   is used exactly, as a normalised origin.  It must be http(s) with no
   credentials, query, fragment or path, and must not name loopback or a
   wildcard.  No Tailscale CLI is consulted.
2. With no public URL, a direct bind is advertised only when
   ``dashboard.server.host`` is this node's own Tailscale address, confirmed
   with ``tailscale ip``.  The edge accepts its own bind address, so that
   origin is always admitted.
3. Anything else -- a loopback or wildcard bind, a LAN bind, a disabled
   server, conflicting or invalid settings -- is *unavailable* with a
   specific reason.  A Tailscale identity proves the host is on a tailnet,
   not that a loopback port is reachable from a phone, so a loopback URL is
   never rewritten to a tailnet address.

``health_check.base_url`` is never consulted: the daemon serves no dashboard
pages, and falling back to it is the bug this module replaced.  "Configured"
is not "reachable": every result reports remote reachability as unverified.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

POSITIVE_TTL_SECONDS = 300.0
NEGATIVE_TTL_SECONDS = 30.0
PROBE_DEADLINE_SECONDS = 2.0
#: Bytes of ``tailscale ip`` output read; a node has a handful of addresses.
_PROBE_OUTPUT_LIMIT = 4096
#: What every result says about reachability from another machine.
REMOTE_REACHABILITY = "unverified"

CANONICAL_KEY = "dashboard.server.public_url"
ALIAS_KEY = "dashboard.public_url"
HOST_KEY = "dashboard.server.host"
ENABLED_KEY = "dashboard.server.enabled"

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_DNS_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_NOTICE = "Remote dashboard link unavailable ({reason}; open it on the daemon host)."

#: The short, secret-free reason each unavailable result puts in a Discord post.
_NOTICE_REASONS = {
    "server_disabled": "the dashboard server is disabled",
    "public_url_conflict": "dashboard.server.public_url and dashboard.public_url disagree",
    "public_url_invalid": "dashboard.server.public_url is not a valid origin",
    "public_url_local": "dashboard.server.public_url names only this machine",
    "not_configured": "no dashboard.server.public_url is configured",
    "tailnet_unconfirmed": "the dashboard's Tailscale address could not be confirmed",
    "probe_failed": "the dashboard link could not be resolved",
}


def _unavailable_notice(reason: str) -> str:
    return _NOTICE.format(reason=_NOTICE_REASONS.get(reason, _NOTICE_REASONS["probe_failed"]))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardLinkSettings:
    """The allowlisted, non-secret settings a resolution reads, and nothing else."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8082
    public_url: str = ""
    #: ``dashboard.public_url`` as written; empty when the alias is unused.
    public_url_alias: str = ""
    #: ``api_auth.trusted_dashboard_origins``, normalised.
    trusted_origins: tuple[str, ...] = ()
    tailscale_path: str = ""

    @classmethod
    def from_config(cls, config: Any) -> DashboardLinkSettings:
        """Read the settings off a loaded ``AppConfig``."""
        server = getattr(config, "dashboard_server", None)
        auth = getattr(config, "api_auth", None)
        return cls._build(server, getattr(auth, "trusted_dashboard_origins", None))

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> DashboardLinkSettings:
        """Read the settings off a parsed ``config.yaml``, as the dashboard server does."""
        from src.config import dashboard_server_config_from_raw

        auth = raw.get("api_auth")
        origins = auth.get("trusted_dashboard_origins") if isinstance(auth, Mapping) else None
        return cls._build(dashboard_server_config_from_raw(raw), origins)

    @classmethod
    def _build(cls, server: Any, origins: Any) -> DashboardLinkSettings:
        if server is None:
            return cls(enabled=False)
        trusted = tuple(
            dict.fromkeys(
                origin
                for origin in (
                    normalise_origin(str(value))
                    for value in (origins if isinstance(origins, (list, tuple)) else ())
                )
                if origin
            )
        )
        try:
            port = int(getattr(server, "port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        return cls(
            enabled=bool(getattr(server, "enabled", True)),
            host=str(getattr(server, "host", "") or ""),
            port=port,
            public_url=str(getattr(server, "public_url", "") or "").strip(),
            public_url_alias=str(getattr(server, "_public_url_alias", "") or "").strip(),
            trusted_origins=trusted,
            tailscale_path=str(getattr(server, "tailscale_path", "") or "").strip(),
        )

    @property
    def public_url_source(self) -> str:
        """The key the effective public URL came from."""
        if self.public_url and self.public_url_alias and self.public_url == self.public_url_alias:
            return ALIAS_KEY
        return CANONICAL_KEY


# ---------------------------------------------------------------------------
# Origin validation
# ---------------------------------------------------------------------------


def normalise_origin(value: str) -> str | None:
    """``scheme://host[:port]`` lower-cased with the default port dropped, else ``None``.

    The same rule as ``src.dashboard_server.settings.normalise_origin`` (the
    edge's), restated here because the daemon's import path must never load
    the dashboard server; ``tests/test_remote_links.py`` keeps them equal.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    if port is None or port == default:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _is_local_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost") or host in _WILDCARD_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _is_host_name(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.split(".")
    return bool(host) and all(
        label
        and len(label) <= 63
        and set(label) <= _DNS_CHARS
        and not label.startswith("-")
        and not label.endswith("-")
        for label in labels
    )


def check_public_url(value: str) -> tuple[str, str, str]:
    """``(origin, reason, problem)`` for a configured public dashboard URL.

    ``origin`` is the normalised origin when the value is usable; otherwise it
    is empty, ``reason`` is ``public_url_invalid`` or ``public_url_local`` and
    ``problem`` says why without echoing the value (it may carry credentials).
    A trailing ``/`` is accepted; any other path is refused, because every
    link appends its own route to the origin.
    """
    text = value.strip()
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - validates the port's syntax and range
    except ValueError:
        return "", "public_url_invalid", "is not a valid URL"
    if parts.scheme not in {"http", "https"}:
        return "", "public_url_invalid", "must start with http:// or https://"
    if not parts.hostname:
        return "", "public_url_invalid", "has no host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "", "public_url_invalid", "must not contain credentials"
    if parts.query or parts.fragment or "?" in text or "#" in text:
        return "", "public_url_invalid", "must not contain a query or fragment"
    if parts.path not in {"", "/"}:
        return "", "public_url_invalid", "must be an origin with no path"
    host = parts.hostname.lower()
    if not _is_host_name(host):
        return "", "public_url_invalid", "has an invalid host name"
    if _is_local_host(host):
        return "", "public_url_local", "names a loopback or wildcard address"
    origin = normalise_origin(f"{parts.scheme}://{parts.netloc}")
    if origin is None:  # pragma: no cover - every refusal above is checked first
        return "", "public_url_invalid", "is not a valid origin"
    return origin, "", ""


def public_urls_conflict(public_url: str, alias: str) -> bool:
    """Both keys set to different origins (a trailing ``/`` or case is no difference)."""
    public_url, alias = public_url.strip(), alias.strip()
    return bool(public_url and alias) and _comparable(public_url) != _comparable(alias)


def _comparable(value: str) -> str:
    return normalise_origin(value.rstrip("/")) or value


def public_url_problem(public_url: str, *, alias: str = "") -> str:
    """Why ``dashboard.server.public_url`` cannot be used, or ``""``; for config validation."""
    if public_urls_conflict(public_url, alias):
        return "conflicts with dashboard.public_url"
    _origin, _reason, problem = check_public_url(public_url)
    return problem


def is_tailnet_address(host: str) -> bool:
    """A Tailscale CGNAT IPv4 or ULA IPv6 literal."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    network = _TAILSCALE_V4 if address.version == 4 else _TAILSCALE_V6
    return address in network


# ---------------------------------------------------------------------------
# The Tailscale probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TailscaleProbe:
    """What ``tailscale ip`` said: ``available``, ``missing``, ``timeout`` or ``error``."""

    status: str
    addresses: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"cli": self.status, "addresses": list(self.addresses), "detail": self.detail}


Probe = Callable[[str], Awaitable[TailscaleProbe]]


def _tailscale_executable(tailscale_path: str) -> tuple[str, str]:
    """``(executable, problem)``: the explicit path, else ``PATH`` -- nothing speculative."""
    if tailscale_path:
        if os.path.isfile(tailscale_path) and os.access(tailscale_path, os.X_OK):
            return tailscale_path, ""
        return "", "dashboard.server.tailscale_path is not an executable file"
    found = shutil.which("tailscale")
    if found:
        return found, ""
    return "", "the tailscale CLI is not on PATH"


async def probe_tailscale(
    tailscale_path: str = "", *, deadline: float = PROBE_DEADLINE_SECONDS
) -> TailscaleProbe:
    """This node's Tailscale addresses, from ``tailscale ip`` under one deadline.

    Runs off the event loop as an async subprocess with bounded output; any
    failure is a status, never an exception.
    """
    executable, problem = _tailscale_executable(tailscale_path)
    if not executable:
        return TailscaleProbe("missing", detail=problem)
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(deadline):
            process = await asyncio.create_subprocess_exec(
                executable,
                "ip",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdout is not None
            output = b""
            while len(output) < _PROBE_OUTPUT_LIMIT:
                chunk = await process.stdout.read(_PROBE_OUTPUT_LIMIT - len(output))
                if not chunk:
                    break
                output += chunk
            code = await process.wait()
    except TimeoutError:
        return TailscaleProbe(
            "timeout", detail=f"tailscale ip did not answer within {deadline:g}s"
        )
    except OSError as error:
        return TailscaleProbe("error", detail=f"tailscale ip could not run ({error.strerror})")
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if code != 0:
        return TailscaleProbe("error", detail=f"tailscale ip exited {code}")
    addresses = tuple(
        dict.fromkeys(
            str(ipaddress.ip_address(line.strip()))
            for line in output.decode("ascii", "replace").splitlines()
            if _parses_as_tailnet(line.strip())
        )
    )
    if not addresses:
        return TailscaleProbe("error", detail="tailscale ip reported no Tailscale address")
    return TailscaleProbe("available", addresses=addresses)


async def _run_probe(probe: Probe, tailscale_path: str) -> TailscaleProbe:
    """``probe``, with any exception turned into an ``error`` status."""
    try:
        return await probe(tailscale_path)
    except Exception as error:  # a probe failure is a reason, not a crash
        logger.debug("tailscale probe failed", exc_info=True)
        return TailscaleProbe("error", detail=f"probe failed: {type(error).__name__}")


def _parses_as_tailnet(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return is_tailnet_address(value)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardLink:
    """The dashboard origin to post, or why there is none.

    ``url`` is an origin with no trailing slash; ``notice`` is the text a
    sender posts instead when ``url`` is empty.  ``edge_compatible`` says
    whether the dashboard server's Host/Origin gates admit ``url`` as its
    configuration stands (``None`` without a URL).  ``tailscale`` is the probe
    this resolution ran, if any.
    """

    url: str = ""
    reason: str = ""
    detail: str = ""
    source: str = ""
    checked_at: float = 0.0
    generation: int = 0
    notice: str = ""
    edge_compatible: bool | None = None
    tailscale: TailscaleProbe | None = None

    @property
    def available(self) -> bool:
        return bool(self.url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
            "source": self.source,
            "checked_at": self.checked_at,
            "generation": self.generation,
            "notice": self.notice,
            "edge_compatible": self.edge_compatible,
            "tailscale": self.tailscale.to_dict() if self.tailscale is not None else None,
            "remote_reachability": REMOTE_REACHABILITY,
        }


def _unavailable(reason: str, detail: str, source: str, **extra: Any) -> DashboardLink:
    return DashboardLink(
        reason=reason,
        detail=detail,
        source=source,
        notice=_unavailable_notice(reason),
        **extra,
    )


def _bind_origin(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}" if port == 80 else f"http://{authority}:{port}"


def _public_edge_compatible(origin: str, settings: DashboardLinkSettings) -> bool:
    """Whether the edge admits a browser on ``origin``, as configured.

    Behind a proxy the edge needs the exact origin trusted (its Host gate and,
    for https, its Origin gate both key on the trusted list); a plain-http
    origin on the concrete bind address is admitted as the server's own.
    """
    if origin in settings.trusted_origins:
        return True
    host = settings.host.lower()
    return host not in _WILDCARD_HOSTS and origin == _bind_origin(host, settings.port)


async def dashboard_link_base(
    settings: DashboardLinkSettings,
    *,
    probe: Probe = probe_tailscale,
    now: float | None = None,
    generation: int = 0,
) -> DashboardLink:
    """Resolve the dashboard origin for ``settings``; never raises.

    The Tailscale CLI is probed only when the answer depends on it: no public
    URL and a concrete bind in the Tailscale ranges.
    """
    checked_at = time.time() if now is None else now
    link = await _resolve(settings, probe)
    return DashboardLink(
        url=link.url,
        reason=link.reason,
        detail=link.detail,
        source=link.source,
        checked_at=checked_at,
        generation=generation,
        notice=link.notice,
        edge_compatible=link.edge_compatible,
        tailscale=link.tailscale,
    )


async def _resolve(settings: DashboardLinkSettings, probe: Probe) -> DashboardLink:
    if not settings.enabled:
        return _unavailable(
            "server_disabled",
            "the dashboard server is disabled (dashboard.server.enabled: false)",
            ENABLED_KEY,
        )
    if settings.public_url or settings.public_url_alias:
        if public_urls_conflict(settings.public_url, settings.public_url_alias):
            return _unavailable(
                "public_url_conflict",
                "dashboard.server.public_url and dashboard.public_url are both set and "
                "disagree; remove one or make them equal",
                ALIAS_KEY,
            )
        source = settings.public_url_source
        origin, reason, problem = check_public_url(settings.public_url)
        if not origin:
            return _unavailable(reason, f"{source} {problem}", source)
        return DashboardLink(
            url=origin,
            reason="public_url",
            detail=f"{source} is set",
            source=source,
            edge_compatible=_public_edge_compatible(origin, settings),
        )

    host = settings.host.lower()
    if _is_local_host(host):
        kind = "wildcard" if host in _WILDCARD_HOSTS else "loopback"
        return _unavailable(
            "not_configured",
            f"{CANONICAL_KEY} is not set and the dashboard server's {kind} bind "
            f"({settings.host}) is not reachable as a remote link; put an authenticated "
            f"tailnet proxy in front of it and set {CANONICAL_KEY} to its origin",
            CANONICAL_KEY,
        )
    if not is_tailnet_address(host):
        return _unavailable(
            "not_configured",
            f"{CANONICAL_KEY} is not set and {HOST_KEY} ({settings.host}) is not a "
            "Tailscale address; set the public URL explicitly",
            CANONICAL_KEY,
        )
    tailscale = await _run_probe(probe, settings.tailscale_path)
    address = str(ipaddress.ip_address(host))
    if tailscale.status != "available":
        return _unavailable(
            "tailnet_unconfirmed",
            f"{HOST_KEY} ({settings.host}) is in the Tailscale range but could not be "
            f"confirmed as this node's address: {tailscale.detail}",
            HOST_KEY,
            tailscale=tailscale,
        )
    if address not in tailscale.addresses:
        return _unavailable(
            "tailnet_unconfirmed",
            f"{HOST_KEY} ({settings.host}) is not one of this node's Tailscale addresses "
            f"({', '.join(tailscale.addresses)})",
            HOST_KEY,
            tailscale=tailscale,
        )
    return DashboardLink(
        url=_bind_origin(address, settings.port),
        reason="tailnet_bind",
        detail=f"{HOST_KEY} is this node's Tailscale address and no public URL is set",
        source=HOST_KEY,
        edge_compatible=True,
        tailscale=tailscale,
    )


class DashboardLinkResolver:
    """Cached :func:`dashboard_link_base` over a live config.

    A result is kept 5 minutes when a URL was found and 30 seconds when not;
    any change to the settings it reads bumps ``generation`` and drops the
    cache at once.  Each change of outcome is logged, without secrets.
    Senders call :meth:`resolve` when rendering a new payload.
    """

    def __init__(
        self,
        config_getter: Callable[[], Any],
        *,
        probe: Probe = probe_tailscale,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_getter = config_getter
        self._probe = probe
        self._clock = clock
        self._wall_clock = wall_clock
        self._settings: DashboardLinkSettings | None = None
        self._generation = 0
        self._cached: DashboardLink | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def current(self) -> DashboardLink | None:
        """The cached result, without resolving."""
        return self._cached

    def _observe(self) -> DashboardLinkSettings:
        settings = DashboardLinkSettings.from_config(self._config_getter())
        if settings != self._settings:
            self._settings = settings
            self._generation += 1
            self._expires_at = 0.0
        return settings

    async def resolve(self) -> DashboardLink:
        settings = self._observe()
        if self._cached is not None and self._clock() < self._expires_at:
            return self._cached
        async with self._lock:
            settings = self._observe()
            if self._cached is not None and self._clock() < self._expires_at:
                return self._cached
            generation = self._generation
            link = await dashboard_link_base(
                settings, probe=self._probe, now=self._wall_clock(), generation=generation
            )
            ttl = POSITIVE_TTL_SECONDS if link.url else NEGATIVE_TTL_SECONDS
            previous, self._cached = self._cached, link
            self._expires_at = self._clock() + ttl
        if previous is None or (previous.url, previous.reason, previous.source) != (
            link.url,
            link.reason,
            link.source,
        ):
            if link.url:
                logger.info(
                    "Dashboard link: %s (%s, from %s; remote reachability unverified)",
                    link.url,
                    link.reason,
                    link.source,
                )
            else:
                logger.info("Dashboard link unavailable (%s): %s", link.reason, link.detail)
        return link


@dataclass(frozen=True, slots=True)
class StaticDashboardLink:
    """A fixed base and notice, for senders built without a resolver (tests, tools)."""

    url: str = ""
    notice: str = field(default_factory=lambda: _unavailable_notice("not_configured"))

    async def resolve(self) -> DashboardLink:
        url = self.url.strip().rstrip("/")
        if url:
            return DashboardLink(url=url, reason="static", source="caller")
        return DashboardLink(reason="static", source="caller", notice=self.notice)


# ---------------------------------------------------------------------------
# Diagnostics (`aq doctor --check dashboard.remote_link`, `aq dashboard link`)
# ---------------------------------------------------------------------------


def edge_detail(link: DashboardLink) -> str:
    """What the dashboard server's Host/Origin gates make of ``link.url``."""
    if link.edge_compatible is None:
        return ""
    if link.edge_compatible:
        return f"the dashboard server's Host/Origin gates admit {link.url}"
    return (
        f"{link.url} is not in api_auth.trusted_dashboard_origins, so the dashboard server "
        "answers 421 misdirected_host or 403 origin_not_allowed through a proxy; add it "
        "and run `aq dashboard restart`"
    )


async def diagnose_dashboard_link(
    settings: DashboardLinkSettings,
    link: DashboardLink | None = None,
    *,
    probe: Probe = probe_tailscale,
) -> dict[str, Any]:
    """The report both diagnostics print: the link, why, and what it depends on.

    Only allowlisted, non-secret settings appear: a public URL is reported as
    set or not (it could carry credentials), never echoed.  The Tailscale CLI
    is probed for the report even when the resolution did not need it.
    """
    if link is None:
        link = await dashboard_link_base(settings, probe=probe)
    tailscale = link.tailscale or await _run_probe(probe, settings.tailscale_path)
    report = link.to_dict()
    report["tailscale"] = tailscale.to_dict()
    report["edge"] = {"compatible": link.edge_compatible, "detail": edge_detail(link)}
    report["settings"] = {
        "enabled": settings.enabled,
        "host": settings.host,
        "port": settings.port,
        "public_url_set": bool(settings.public_url),
        "public_url_alias_set": bool(settings.public_url_alias),
        "trusted_dashboard_origins": list(settings.trusted_origins),
        "tailscale_path_set": bool(settings.tailscale_path),
    }
    return report
