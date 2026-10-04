"""``dashboard.server.*`` doctor checks -- is the dashboard server up, current and private?

docs/specs/dashboard-server.md §1 ("Observers").  ``aq doctor`` runs inside
the daemon, and the daemon's import path must never load the dashboard
server (§1, "Import boundary"), so nothing here imports
``src.dashboard_server``: the checks read ``config.dashboard_server`` and the
installed manifest, and ask the process who it is over HTTP
(``GET /__aq/health``).  The fix is the operator's own command, run as a
subprocess -- ``aq dashboard start`` (or ``restart`` for a stale build) -- so
the dashboard server is never the daemon's child to supervise.

* ``dashboard.server.running``  -- missing, crashed, unresponsive or serving an
  older build than the one installed; fixable.
* ``dashboard.server.bundle``   -- no bundle, an unreadable manifest, or one
  built for the daemon's retired ``/dashboard`` mount; the fix is a rebuild,
  which needs Node.js and minutes, so it is named rather than run.
* ``dashboard.server.port``     -- the configured port answers as something else.
* ``dashboard.server.exposure`` -- a non-loopback bind, and what that hands out.
* ``dashboard.remote_link``     -- the origin Discord links name (or why they
  carry a notice), whether the edge answers for it, and that reaching it from
  another device is unverified.  Uses :mod:`src.remote_links`, the resolver
  every sender uses; no dashboard-server import.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "dashboard-server"

RUNNING = "dashboard.server.running"
BUNDLE = "dashboard.server.bundle"
PORT = "dashboard.server.port"
EXPOSURE = "dashboard.server.exposure"
REMOTE_LINK = "dashboard.remote_link"
PUBLIC_URL = "dashboard.public_url"

#: What the dashboard server's ``/__aq/health`` names itself
#: (``src.dashboard_server.process.SERVICE_NAME``; a test keeps them equal).
SERVICE_NAME = "aq-dashboard-server"
MANIFEST_NAME = "aq-dashboard-manifest.json"
#: ``src.cli.daemon.DASHBOARD_SERVER_PID_FILE``, by name.
PID_FILE = Path.home() / ".agent-queue" / "dashboard-server.pid"
REBUILD_COMMAND = "aq install --restart-from dashboard.build"

_PROBE_SECONDS = 3.0
#: `aq dashboard start` waits up to 10 s for the server, restart up to 20 s.
_FIX_SECONDS = 45.0

_WILDCARDS = {"0.0.0.0", "::"}


# ---------------------------------------------------------------------------
# What the checks read
# ---------------------------------------------------------------------------


def _section(ctx: DoctorContext) -> Any:
    return getattr(ctx.config, "dashboard_server", None)


def _url(section: Any) -> str:
    host = str(section.host)
    if host in _WILDCARDS:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{section.port}/"


def _bundle_directory() -> Path:
    """``src/dashboard_assets/dist``, located without importing anything but the data package."""
    import importlib.util

    spec = importlib.util.find_spec("src.dashboard_assets")
    locations = list(spec.submodule_search_locations or ()) if spec else []
    base = Path(locations[0]) if locations else Path(__file__).resolve().parents[1] / "dashboard_assets"
    return base / "dist"


def _manifest(directory: Path | None = None) -> tuple[dict[str, Any] | None, str | None, str]:
    """``(payload, sha256, problem)`` for the installed manifest; payload ``None`` if absent."""
    path = (directory or _bundle_directory()) / MANIFEST_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None, ""
    except OSError as error:
        return None, None, f"cannot read {path}: {error}"
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, digest, f"{path} is not valid JSON: {error}"
    if not isinstance(payload, dict):
        return None, digest, f"{path} is not a manifest"
    return payload, digest, ""


def _probe(url: str) -> tuple[str, dict[str, Any] | None]:
    """``("ours", identity)``, ``("foreign", None)`` or ``("none", None)``; blocking."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/__aq/health", timeout=_PROBE_SECONDS) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as error:
        error.close()
        return "foreign", None
    except (urllib.error.URLError, OSError, ValueError):
        return "none", None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "foreign", None
    if isinstance(payload, dict) and payload.get("service") == SERVICE_NAME:
        return "ours", payload
    return "foreign", None


async def _probe_async(url: str) -> tuple[str, dict[str, Any] | None]:
    return await asyncio.to_thread(_probe, url)


def _recorded_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _info(check_id: str, detail: str, **data: Any) -> CheckResult:
    return CheckResult(id=check_id, severity=Severity.INFO, detail=detail, data=data)


def _unmanaged(ctx: DoctorContext, check_id: str) -> CheckResult | None:
    """INFO when there is no dashboard server to check: disabled, or no bundle (Vite)."""
    section = _section(ctx)
    if section is None or not getattr(section, "enabled", False):
        return _info(check_id, "the dashboard server is disabled (dashboard.server.enabled: false)")
    payload, _digest, problem = _manifest()
    if payload is None and not problem:
        return _info(
            check_id,
            "no dashboard bundle is installed (a source checkout); the Vite dev server "
            "(`npm -w dashboard run dev`) serves the dashboard here",
        )
    return None


# ---------------------------------------------------------------------------
# dashboard.server.running
# ---------------------------------------------------------------------------


async def _check_running(ctx: DoctorContext) -> CheckResult:
    unmanaged = _unmanaged(ctx, RUNNING)
    if unmanaged is not None:
        return unmanaged
    section = _section(ctx)
    url = _url(section)
    kind, identity = await _probe_async(url)
    data: dict[str, Any] = {"url": url}
    if kind == "ours" and identity is not None:
        bundle = identity.get("bundle") if isinstance(identity.get("bundle"), dict) else {}
        data.update(pid=identity.get("pid"), bundle=bundle, upstream_ok=identity.get("upstream_ok"))
        _payload, installed, _problem = _manifest()
        served = bundle.get("manifest_sha256")
        if installed and served and served != installed:
            data["fix"] = "restart"
            return CheckResult(
                id=RUNNING,
                severity=Severity.WARN,
                detail=(
                    f"the dashboard server at {url} (PID {identity.get('pid')}) serves an older "
                    "build than the one installed; `aq dashboard restart` serves the new one"
                ),
                fixable=True,
                data=data,
            )
        return CheckResult(
            id=RUNNING,
            severity=Severity.OK,
            detail=f"running at {url} (PID {identity.get('pid')}), bundle {bundle.get('version')}",
            data=data,
        )
    if kind == "foreign":
        return CheckResult(
            id=RUNNING,
            severity=Severity.WARN,
            detail=(
                f"the dashboard server is not running: port {section.port} is held by another "
                "program (see dashboard.server.port)"
            ),
            data=data,
        )
    pid = _recorded_pid()
    data["fix"] = "start"
    if pid is not None and _alive(pid):
        data["fix"] = "restart"
        data["pid"] = pid
        detail = (
            f"the dashboard server (PID {pid}) is running but does not answer at {url}; "
            "`aq dashboard restart` starts it again"
        )
    elif pid is not None:
        data["stale_pid"] = pid
        detail = (
            f"the dashboard server is not running: it crashed or was killed (its PID file names "
            f"PID {pid}, which has exited); `aq dashboard start` starts it"
        )
    else:
        detail = f"the dashboard server is not running at {url}; `aq dashboard start` starts it"
    return CheckResult(id=RUNNING, severity=Severity.WARN, detail=detail, fixable=True, data=data)


def _aq_executable() -> str | None:
    beside = Path(sys.executable).with_name("aq")
    if beside.exists():
        return str(beside)
    return shutil.which("aq")


async def _run_aq(*args: str) -> tuple[int, str]:
    """Run the operator's own ``aq`` command; the daemon never supervises the server."""
    aq = _aq_executable()
    if aq is None:
        return 127, "the `aq` command was not found beside the daemon's interpreter or on PATH"
    process = await asyncio.create_subprocess_exec(
        aq, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=_FIX_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"`aq {' '.join(args)}` did not finish within {_FIX_SECONDS:.0f}s"
    return process.returncode or 0, output.decode("utf-8", "replace").strip()


async def _fix_running(ctx: DoctorContext) -> CheckResult:
    before = await _check_running(ctx)
    action = before.data.get("fix")
    if before.severity is not Severity.WARN:
        return before
    if action not in {"start", "restart"}:
        # A port held by another program: starting would only fail on it.
        raise RuntimeError(
            "nothing to start: the port is held by another program; stop it or set "
            "dashboard.server.port"
        )
    code, output = await _run_aq("dashboard", action)
    if code != 0:
        last = output.splitlines()[-1] if output else f"exit status {code}"
        raise RuntimeError(f"`aq dashboard {action}` failed: {last}")
    return await _check_running(ctx)


# ---------------------------------------------------------------------------
# dashboard.server.bundle
# ---------------------------------------------------------------------------


async def _check_bundle(ctx: DoctorContext) -> CheckResult:
    section = _section(ctx)
    if section is None or not getattr(section, "enabled", False):
        return _info(BUNDLE, "the dashboard server is disabled (dashboard.server.enabled: false)")
    payload, digest, problem = _manifest()
    if payload is None and not problem:
        return _info(
            BUNDLE,
            "no dashboard bundle is installed (a source checkout); build one with "
            f"`{REBUILD_COMMAND}`, or run the Vite dev server",
        )
    if problem:
        return CheckResult(
            id=BUNDLE, severity=Severity.WARN,
            detail=f"the dashboard bundle's manifest is unusable ({problem}); rebuild it with "
                   f"`{REBUILD_COMMAND}`",
        )
    files = payload.get("files")
    if not isinstance(files, dict) or "index.html" not in files:
        return CheckResult(
            id=BUNDLE, severity=Severity.WARN,
            detail=f"the dashboard bundle lists no index.html; rebuild it with `{REBUILD_COMMAND}`",
        )
    if "base" not in payload:
        return CheckResult(
            id=BUNDLE, severity=Severity.WARN,
            detail=(
                "the dashboard bundle was built for the daemon's retired /dashboard mount and the "
                f"dashboard server will not serve it; rebuild it with `{REBUILD_COMMAND}`"
            ),
        )
    if payload.get("base") != "/":
        return CheckResult(
            id=BUNDLE, severity=Severity.WARN,
            detail=(
                f"the dashboard bundle was built for base {payload.get('base')!r}, not '/'; "
                f"rebuild it with `{REBUILD_COMMAND}`"
            ),
        )
    return CheckResult(
        id=BUNDLE, severity=Severity.OK,
        detail=f"bundle {payload.get('version')} ({len(files)} files) built for /",
        data={"version": payload.get("version"), "files": len(files), "manifest_sha256": digest},
    )


# ---------------------------------------------------------------------------
# dashboard.server.port
# ---------------------------------------------------------------------------


async def _check_port(ctx: DoctorContext) -> CheckResult:
    unmanaged = _unmanaged(ctx, PORT)
    if unmanaged is not None:
        return unmanaged
    section = _section(ctx)
    url = _url(section)
    kind, _identity = await _probe_async(url)
    if kind == "foreign":
        return CheckResult(
            id=PORT, severity=Severity.WARN,
            detail=(
                f"port {section.port} answers, but not as the dashboard server: another program "
                "holds it. Stop that program or set dashboard.server.port, then `aq dashboard "
                "start` (the port is never picked automatically)"
            ),
            data={"url": url},
        )
    return CheckResult(
        id=PORT, severity=Severity.OK,
        detail=f"port {section.port} is " + ("the dashboard server's" if kind == "ours" else "free"),
        data={"url": url},
    )


# ---------------------------------------------------------------------------
# dashboard.server.exposure
# ---------------------------------------------------------------------------


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _check_exposure(ctx: DoctorContext) -> CheckResult:
    section = _section(ctx)
    if section is None or not getattr(section, "enabled", False):
        return _info(EXPOSURE, "the dashboard server is disabled (dashboard.server.enabled: false)")
    host = str(section.host)
    if _loopback(host):
        return CheckResult(
            id=EXPOSURE, severity=Severity.OK,
            detail=f"bound to {host}: not reachable from another machine",
        )
    return CheckResult(
        id=EXPOSURE, severity=Severity.WARN,
        detail=(
            f"dashboard.server.host is {host}: anyone who can reach port {section.port} gets the "
            "operator console -- every /api route with local-operator scope, with no login "
            "(remote terminals require trusted origins; bearer tokens stay loopback-only). "
            "Prefer 127.0.0.1 and "
            f"`ssh -L {section.port}:127.0.0.1:{section.port}`; see "
            "docs/specs/dashboard-server.md §3.4"
        ),
        data={"host": host, "port": section.port},
    )


# ---------------------------------------------------------------------------
# dashboard.remote_link
# ---------------------------------------------------------------------------

_HEALTH = {"ours": "running", "foreign": "port_held_by_another_program", "none": "not_running"}
REMOTE_GUIDE = "docs/guides/dashboard.md#dashboard-links-in-discord-posts"


async def _check_remote_link(ctx: DoctorContext) -> CheckResult:
    from src.remote_links import LinkSettings, describe_link, resolve_dashboard_link

    settings = LinkSettings.from_config(ctx.config)
    link = await resolve_dashboard_link(settings)
    health = "disabled"
    if settings.enabled:
        kind, _identity = await _probe_async(_url(_section(ctx)))
        health = _HEALTH[kind]
    discord = getattr(ctx.config, "discord", None)
    posting = bool(getattr(discord, "channel_id", "") or "")
    data = describe_link(
        settings, link, server_health=health, discord_channel_configured=posting,
    )
    if not settings.enabled:
        return CheckResult(
            id=REMOTE_LINK, severity=Severity.INFO,
            detail=(
                "the dashboard server is disabled (dashboard.server.enabled: false); external "
                "posts carry the unavailable notice"
            ),
            data=data,
        )
    if link.url:
        edge = data["edge"]
        where = f"external dashboard links name {link.url} (from {link.source})"
        if not (edge["host_allowed"] and edge["origin_allowed"]):
            return CheckResult(
                id=REMOTE_LINK, severity=Severity.WARN,
                detail=(
                    f"{where}, but the dashboard server's edge refuses that origin: add "
                    f"{link.url} to api_auth.trusted_dashboard_origins, restart the daemon "
                    f"and `aq dashboard restart`; see {REMOTE_GUIDE}"
                ),
                data=data,
            )
        return CheckResult(
            id=REMOTE_LINK, severity=Severity.OK,
            detail=f"{where}; reaching it from another device is unverified",
            data=data,
        )
    detail = f"external dashboard links are unavailable: {link.detail}"
    if not posting:
        return CheckResult(
            id=REMOTE_LINK, severity=Severity.INFO,
            detail=f"{detail} (nothing posts: discord.channel_id is not set)",
            data=data,
        )
    return CheckResult(
        id=REMOTE_LINK, severity=Severity.WARN,
        detail=(
            f"{detail}. Discord posts carry a notice instead of a link. Set "
            f"dashboard.server.public_url to your authenticated tailnet proxy's origin; see "
            f"{REMOTE_GUIDE}"
        ),
        data=data,
    )


# ---------------------------------------------------------------------------
# dashboard.public_url
# ---------------------------------------------------------------------------

#: Spec 2026-10-03 §6.3 option 2, the answer to its §8 Q1: the dashboard server
#: behind Tailscale Serve, so a phone on the tailnet gets HTTPS on a MagicDNS
#: name that survives a port change, with nothing exposed publicly.
TAILSCALE_SERVE_STEPS = (
    "run `tailscale serve --bg --https=443 http://127.0.0.1:{port}`, set "
    "dashboard.server.public_url to the https://<machine>.<tailnet>.ts.net origin it "
    "prints, add that origin to api_auth.trusted_dashboard_origins, then restart the "
    "daemon and `aq dashboard restart`"
)


def _reason(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timed out"
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    return str(error) or type(error).__name__


def _focus_shell_probe(origin: str, timeout: float = _PROBE_SECONDS) -> tuple[int, str]:
    """``(status, why)`` for ``GET <origin>/focus``; blocking.

    ``why`` is empty when the dashboard shell answered (200, HTML).  A
    host:port that is not listening is told apart from one that answers
    wrongly: it is the first thing to fix, and a different fix.
    """
    host, port = "", 0
    try:
        parts = urllib.parse.urlsplit(origin)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except (OSError, ValueError) as error:
        return 0, f"{host}:{port} is not listening ({_reason(error)})"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{origin}/focus", timeout=timeout) as response:
            status = response.getcode()
            content_type = response.headers.get("Content-Type", "") or ""
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, f"GET /focus answered {error.code}"
    except (urllib.error.URLError, OSError) as error:
        cause = getattr(error, "reason", None)
        reason = _reason(cause if isinstance(cause, BaseException) else error)
        return 0, f"GET /focus failed ({reason})"
    if status != 200:
        return status, f"GET /focus answered {status}"
    if "html" not in content_type.lower():
        return status, "GET /focus did not answer with the dashboard's HTML shell"
    return status, ""


async def _check_public_url(ctx: DoctorContext) -> CheckResult:
    """Spec §6.3: set, HTTPS (Tailscale Serve) or a tailnet address, listening, and
    ``GET /focus`` returns the shell.  Plain HTTP works on the tailnet but is not
    the approach chosen in §8 Q1, so it warns with the steps to move."""
    from src.remote_links import (
        DashboardLink,
        LinkSettings,
        edge_compatibility,
        is_tailnet_address,
        normalise_public_origin,
    )

    settings = LinkSettings.from_config(ctx.config)
    if not settings.enabled:
        return _info(
            PUBLIC_URL, "the dashboard server is disabled (dashboard.server.enabled: false)"
        )
    serve = TAILSCALE_SERVE_STEPS.format(port=settings.port)
    if not settings.public_url.strip():
        return CheckResult(
            id=PUBLIC_URL,
            severity=Severity.WARN,
            detail=(
                "dashboard.server.public_url is not set, so Discord posts carry no dashboard "
                f"link. To open the dashboard from a phone on the tailnet: {serve}"
            ),
            data={"public_url": None},
        )
    origin, why = normalise_public_origin(settings.public_url)
    if not origin:
        return CheckResult(
            id=PUBLIC_URL,
            severity=Severity.WARN,
            detail=f"dashboard.server.public_url {why}. {serve[0].upper()}{serve[1:]}",
            data={"public_url": None, "why": why},
        )
    parts = urllib.parse.urlsplit(origin)
    host = (parts.hostname or "").lower()
    https = parts.scheme == "https"
    tailnet = is_tailnet_address(host) or host.endswith(".ts.net")
    data: dict[str, Any] = {"public_url": origin, "https": https, "tailnet": tailnet}

    # An HTTPS origin is a proxy in front of the dashboard server, and an origin on
    # the server's own port is the server itself: either way the edge's Host and
    # Origin gates apply, and an origin they refuse answers 421 or 403.
    if https or parts.port == settings.port:
        edge = edge_compatibility(DashboardLink(url=origin), settings)
        data["edge"] = edge
        if not (edge["host_allowed"] and edge["origin_allowed"]):
            return CheckResult(
                id=PUBLIC_URL,
                severity=Severity.WARN,
                detail=(
                    f"{origin}: the dashboard server's edge refuses this origin (421/403) "
                    "until it is listed in api_auth.trusted_dashboard_origins; add it, "
                    "restart the daemon and `aq dashboard restart`"
                ),
                data=data,
            )

    status, failure = await asyncio.to_thread(_focus_shell_probe, origin)
    data["focus_status"] = status
    if failure:
        detail = f"dashboard.server.public_url {origin}: {failure}"
        return CheckResult(
            id=PUBLIC_URL,
            severity=Severity.WARN,
            detail=detail if https else f"{detail}. {serve[0].upper()}{serve[1:]}",
            data=data,
        )
    if not https:
        where = "a tailnet address" if tailnet else "a host outside the tailnet"
        return CheckResult(
            id=PUBLIC_URL,
            severity=Severity.WARN,
            detail=(
                f"{origin} serves the dashboard over plain HTTP on {where}. Remote access "
                f"is meant to go through Tailscale Serve HTTPS (spec §6.3): {serve}"
            ),
            data=data,
        )
    proxy = "" if tailnet else " (not a tailnet name: the proxy must authenticate)"
    return CheckResult(
        id=PUBLIC_URL,
        severity=Severity.OK,
        detail=f"{origin} serves the dashboard shell at /focus over HTTPS{proxy}",
        data=data,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def dashboard_server_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=RUNNING, run=_check_running, fix=_fix_running,
            timeout_s=_FIX_SECONDS + 10, owner=OWNER,
        ),
        DoctorCheck(id=BUNDLE, run=_check_bundle, owner=OWNER),
        DoctorCheck(id=PORT, run=_check_port, timeout_s=10.0, owner=OWNER),
        DoctorCheck(id=EXPOSURE, run=_check_exposure, owner=OWNER),
        # The Tailscale probe's 2 s deadline plus the health probe's 3 s.
        DoctorCheck(id=REMOTE_LINK, run=_check_remote_link, timeout_s=10.0, owner=OWNER),
        # Connect, then GET /focus: two probe deadlines, plus slack.
        DoctorCheck(
            id=PUBLIC_URL, run=_check_public_url, timeout_s=_PROBE_SECONDS + 5, owner=OWNER
        ),
    ]


CHECKS = dashboard_server_checks()
