"""Build the dashboard, serve it from the dashboard server, and open it once.

The daemon is API-only (docs/specs/dashboard-server.md): the dashboard is a
verified bundle served by its own process, the dashboard server, which also
proxies the daemon's API so the browser stays same-origin.  A release wheel
carries that bundle.  A source checkout -- which is what the one-command
bootstrap installs -- carries only the TypeScript, and the installer used to
finish by telling a newcomer to run a Vite dev server by hand.  That is an extra
step the one-command install exists to remove, so these three steps close it:

* ``dashboard.build`` produces the same verified bundle a release ships, using
  the release's own staging script (``scripts/build_release_artifact.py``).  The
  bundle is tied to the checkout it was built from, so a rerun after the
  bootstrap pulled an update rebuilds it, and a rerun with nothing new does no
  work at all.  It touches no process.
* ``dashboard.serve`` runs ``aq dashboard start`` and proves the result: the
  dashboard server answers ``/__aq/health`` as ours with a verified bundle --
  the one this install has, not an older build -- and ``GET /`` is ``200``.
  A server still serving an older build is restarted; that is also what moves
  an install that relied on the daemon's old ``/dashboard`` mount across.
* ``dashboard.open`` opens the dashboard server's URL in the user's browser --
  once, on the first interactive install that reaches it, and never from an
  unattended run, which the installation contract forbids from opening one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .command import CommandOutput, CommandRunner, run_command
from .node_toolchain import Fetcher, ToolchainError, download, ensure_toolchain
from .onboarding import (
    CAPABILITY_DAEMON,
    DASHBOARD_DISABLED,
    DASHBOARD_MISCONFIGURED,
    DASHBOARD_PORT_CONFLICT,
    DASHBOARD_SERVER,
    DASHBOARD_UNBUILT,
    STEP_CHECK,
    STEP_DAEMON,
    STEP_DASHBOARD,
    DashboardInfo,
    HttpProbe,
    IdentityProbe,
    _nobody,
    aq_aware_which,
    config_path_for,
    dashboard_server_identity,
    http_status,
    inspect_dashboard,
)
from .redaction import redact
from .results import StepResult
from .state import default_state_dir
from .steps import StepContext, StepSpec

STEP_DASHBOARD_BUILD = "dashboard.build"
STEP_DASHBOARD_SERVE = "dashboard.serve"
STEP_DASHBOARD_OPEN = "dashboard.open"

#: Where each build's full output goes, under AQ's data directory.
BUILD_LOG_NAME = "dashboard-build.log"
#: How many lines of a failed stage's output the step result quotes.
FAILURE_TAIL_LINES = 12

#: `npm ci` downloads the whole workspace on a first run.
NPM_INSTALL_TIMEOUT = 1200.0
#: Type-checking and bundling the dashboard.
BUILD_TIMEOUT = 900.0
#: `aq dashboard start` waits up to 10 s for the server's identity, and a
#: restart first waits up to 10 s for the old one to exit.
SERVE_TIMEOUT = 60.0

#: Equal to ``src.dashboard_server.process``'s names, read here without
#: importing it (this module is imported by `aq update` before the code moves).
MANIFEST_NAME = "aq-dashboard-manifest.json"
SERVER_LOG_NAME = "dashboard-server.log"
#: `aq dashboard start`'s exit status for "no bundle is installed here".
EXIT_NO_BUNDLE = 2

#: What a checkout with no bundle is told, as `aq dashboard status` tells it.
NO_BUNDLE_HINT = (
    "no dashboard bundle is installed, so there is nothing to serve: run "
    "`npm -w dashboard run dev` for the Vite dev server, or build one with "
    "`aq install --restart-from dashboard.build`"
)

#: Records which checkout state the staged bundle was built from.  It lives
#: beside ``dist/``, not inside it, so it is never served or covered by the
#: bundle's integrity manifest.
STAMP_NAME = ".aq-dashboard-source"

#: Paths whose changes mean the bundle must be rebuilt.
BUILD_INPUTS = (
    "dashboard",
    "packages/aq-ts-client",
    "openapi.json",
    "package.json",
    "package-lock.json",
)

#: Files that identify a checkout this step can build from.
_CHECKOUT_MARKERS = (
    "package.json",
    "package-lock.json",
    "dashboard/package.json",
    "scripts/build_release_artifact.py",
)


def source_checkout_root(start: Path | None = None) -> Path | None:
    """The source checkout this installer runs from, or ``None`` for a release."""
    root = start if start is not None else Path(__file__).resolve().parents[2]
    return root if all((root / marker).is_file() for marker in _CHECKOUT_MARKERS) else None


def bundle_directory(root: Path) -> Path:
    return root / "src" / "dashboard_assets" / "dist"


def stamp_path(root: Path) -> Path:
    return root / "src" / "dashboard_assets" / STAMP_NAME


def source_fingerprint(root: Path, *, git: str | None, execute: CommandRunner) -> str | None:
    """Identify the build inputs the dashboard would be built from.

    The committed object ids of the build inputs plus any uncommitted change to
    them, so a pulled update that touches the dashboard and a local edit both
    trigger a rebuild -- and an update that touches nothing the dashboard is
    built from does not spend another `npm ci`.  ``None`` when Git cannot say,
    and then an existing verified bundle is trusted rather than rebuilt forever.
    """
    if not git:
        return None
    committed = execute([git, "-C", str(root), "ls-tree", "HEAD", "--", *BUILD_INPUTS])
    if not committed.ok:
        return None
    changes = execute(
        [git, "-C", str(root), "status", "--porcelain", "--", *BUILD_INPUTS]
    )
    if not changes.ok:
        return None
    digest = hashlib.sha256((committed.out + "\0" + changes.out).encode("utf-8"))
    return f"inputs-{digest.hexdigest()[:32]}"


def _bundle_verifies(root: Path) -> bool:
    from src.dashboard_server.bundle import verify_dashboard_bundle

    try:
        verify_dashboard_bundle(bundle_directory(root))
    except ValueError:
        return False
    return True


def bundle_is_current(root: Path, fingerprint: str | None) -> bool:
    """A verified bundle exists and was built from this checkout state."""
    if not _bundle_verifies(root):
        return False
    if fingerprint is None:
        return True
    try:
        return stamp_path(root).read_text(encoding="utf-8").strip() == fingerprint
    except OSError:
        return False


def failure_excerpt(output: CommandOutput, limit: int = FAILURE_TAIL_LINES) -> str:
    """The lines of a failed build worth showing: its errors, else its tail.

    A build that fails inside `tsc` or Vite reports why on stdout, and the
    staging script's own traceback on stderr says only that npm exited non-zero
    -- which is all a first macOS install ever got to see.
    """
    lines = [line.rstrip() for line in (output.stdout + "\n" + output.stderr).splitlines()]
    lines = [line for line in lines if line.strip()]
    errors = [
        line
        for line in lines
        if "error" in line.lower() and not line.lstrip().startswith("at ")
    ]
    chosen = (errors or lines)[-limit:]
    if not chosen and output.error:
        chosen = [output.error]
    return "\n".join(chosen)


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    ok: bool
    summary: str = ""
    remediation: str = ""
    log: str | None = None


def build_bundle(
    checkout: Path,
    *,
    state_dir: Path,
    system: str,
    arch: str,
    execute: CommandRunner,
    fetch: Fetcher = download,
    interpreter: str | None = None,
    environ: Mapping[str, str] | None = None,
    fingerprint: str | None = None,
    rerun: str = "rerun the command",
) -> BuildOutcome:
    """Build and stage the verified dashboard bundle for *checkout*.

    Shared by `aq install` (``dashboard.build``) and `aq update`, so both build
    with the same pinned Node.js, the same stages and the same log.  *rerun*
    names the command a failure tells the person to run again.
    """
    # The pinned, checksum-verified Node.js AQ owns -- never the machine's.
    try:
        toolchain = ensure_toolchain(state_dir, system, arch, fetch=fetch)
    except ToolchainError as error:
        return BuildOutcome(
            False,
            f"could not provide Node.js for the dashboard build: {error}",
            (
                "Check this machine can reach https://nodejs.org (a proxy or firewall is "
                f"the usual cause), then {rerun}; it retries the download."
            ),
        )

    env = dict(os.environ if environ is None else environ)
    env["PATH"] = os.pathsep.join([str(toolchain.bin), env.get("PATH", "")])
    npm = str(toolchain.npm)
    stages = (
        (
            "install the dashboard's packages",
            # `--include=dev`: the build tools are devDependencies, and a
            # machine with NODE_ENV=production or `omit=dev` in its npmrc
            # would otherwise leave them out.
            [npm, "ci", "--include=dev", "--no-audit", "--no-fund", "--loglevel=error"],
            NPM_INSTALL_TIMEOUT,
        ),
        (
            "generate the dashboard's API client",
            [npm, "-w", "@aq/ts-client", "run", "generate"],
            BUILD_TIMEOUT,
        ),
        (
            "build and stage the dashboard",
            [
                interpreter or sys.executable,
                str(checkout / "scripts" / "build_release_artifact.py"),
                "--project-root",
                str(checkout),
            ],
            BUILD_TIMEOUT,
        ),
    )
    log = state_dir / BUILD_LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    for label, argv, timeout in stages:
        output = execute(argv, timeout=timeout, env=env, cwd=str(checkout))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(argv)}\n{output.stdout}{output.stderr}\n")
        if not output.ok:
            return BuildOutcome(
                False,
                redact(f"could not {label}:\n{failure_excerpt(output)}"),
                f"The full output is in {log}. Fix what it names, then {rerun}; it retries "
                "the build.",
                str(log),
            )

    if not _bundle_verifies(checkout):
        return BuildOutcome(
            False,
            "the dashboard build finished but its bundle does not verify",
            f"{rerun[0].upper()}{rerun[1:]} to rebuild it.",
            str(log),
        )
    if fingerprint is not None:
        stamp_path(checkout).write_text(fingerprint + "\n", encoding="utf-8")
    return BuildOutcome(True, "built the dashboard", log=str(log))


def dashboard_build_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    root: Path | None = None,
    python: str | None = None,
    fetch: Fetcher = download,
    depends_on: tuple[str, ...] = (STEP_CHECK, STEP_DAEMON),
) -> StepSpec:
    """Build the dashboard bundle from a source checkout.

    It starts, stops and restarts nothing: the daemon no longer serves the
    dashboard, and ``dashboard.serve`` owns the dashboard server -- including
    restarting one that still serves the build this step just replaced.
    """
    lookup = which or shutil.which
    execute = runner or run_command
    state_dir = home or default_state_dir(environ)
    interpreter = python or sys.executable

    def _root() -> Path | None:
        return source_checkout_root(root)

    def _fingerprint(checkout: Path) -> str | None:
        return source_fingerprint(checkout, git=lookup("git"), execute=execute)

    def run(context: StepContext) -> StepResult:
        checkout = _root()
        if checkout is None:
            return StepResult.succeeded(
                STEP_DASHBOARD_BUILD,
                "this installation ships its dashboard; nothing to build",
                detail={"built": False, "source_checkout": False},
            )
        fingerprint = _fingerprint(checkout)
        detail: dict[str, object] = {"built": False, "source_checkout": True}
        if bundle_is_current(checkout, fingerprint):
            return StepResult.succeeded(
                STEP_DASHBOARD_BUILD, "the dashboard is already built for this checkout",
                detail=detail,
            )

        outcome = build_bundle(
            checkout,
            state_dir=state_dir,
            system=context.facts.system,
            arch=context.facts.arch,
            execute=execute,
            fetch=fetch,
            interpreter=interpreter,
            environ=environ,
            fingerprint=fingerprint,
            rerun="rerun the install command",
        )
        if not outcome.ok:
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                outcome.summary,
                outcome.remediation,
                detail={**detail, **({"log": outcome.log} if outcome.log else {})},
            )
        return StepResult.succeeded(
            STEP_DASHBOARD_BUILD, "built the dashboard", detail={**detail, "built": True}
        )

    def verify(context: StepContext) -> bool:
        checkout = _root()
        if checkout is None:
            return True
        # A bundle built for the daemon's old /dashboard mount has no `base`
        # in its manifest and no longer verifies, so it is rebuilt here.
        return bundle_is_current(checkout, _fingerprint(checkout))

    return StepSpec(
        id=STEP_DASHBOARD_BUILD,
        title="Build the dashboard",
        description=(
            "From a source checkout, builds the same verified dashboard bundle a release "
            "ships, for the dashboard server to serve. Rebuilds only when the checkout changed."
        ),
        run=run,
        depends_on=depends_on,
        mutating=True,
        consent_prompt="Build the dashboard now? (downloads its packages; a few minutes)",
        verify=verify,
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# dashboard.serve -- the dashboard server, started and proven
# ---------------------------------------------------------------------------


def installation_root(root: Path | None = None) -> Path:
    """The directory holding this installation's ``src`` package.

    A source checkout's root, or a release's ``site-packages``: the wheel
    installs ``src`` as a top-level package, so either way the bundle is at
    ``src/dashboard_assets/dist`` under it -- the directory the dashboard
    server serves.
    """
    return root if root is not None else Path(__file__).resolve().parents[2]


def bundle_present(root: Path | None = None) -> bool:
    """A dashboard bundle is installed: a release's, or one ``dashboard.build`` staged."""
    return (bundle_directory(installation_root(root)) / MANIFEST_NAME).is_file()


def serves_installed_build(identity: Mapping[str, Any] | None, root: Path | None = None) -> bool:
    """The dashboard server's ``/__aq/health`` names a verified bundle, and it is this install's.

    A server started before a rebuild keeps serving the manifest it verified
    at startup, and the rebuilt assets have new content-hashed names, so it
    would serve a page whose scripts 404.  When either digest is unknown the
    server's own verification is trusted.
    """
    from src.dashboard_server.process import installed_manifest_sha256

    bundle = identity.get("bundle") if identity else None
    if not isinstance(bundle, Mapping) or bundle.get("verified") is not True:
        return False
    served = bundle.get("manifest_sha256")
    installed = installed_manifest_sha256(bundle_directory(installation_root(root)))
    return not served or installed is None or served == installed


@dataclass(frozen=True, slots=True)
class _Observed:
    """One look at the dashboard server: the report, and the identity behind it."""

    info: DashboardInfo
    identity: dict[str, Any] | None
    serving: bool


def _observe(
    config: Path, *, probe: HttpProbe, identify: IdentityProbe, root: Path | None
) -> _Observed:
    seen: dict[str, Any] = {}

    def recording(url: str) -> tuple[str, dict[str, Any] | None]:
        kind, identity = identify(url)
        seen["identity"] = identity
        return kind, identity

    info = inspect_dashboard(
        config, probe=probe, identify=recording, bundle_present=lambda: bundle_present(root)
    )
    identity = seen.get("identity")
    serving = (
        info.source == DASHBOARD_SERVER
        and info.reachable
        and serves_installed_build(identity, root)
    )
    return _Observed(info, identity, serving)


def _start_failure(output: CommandOutput) -> str:
    """Why `aq --json dashboard start` failed, with the server's own last words."""
    try:
        envelope = json.loads(output.stdout)
    except ValueError:
        envelope = None
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return output.message()
    reason = str(error.get("message") or output.message())
    details = error.get("details")
    excerpt = details.get("log_excerpt") if isinstance(details, dict) else None
    if isinstance(excerpt, list) and excerpt:
        tail = "\n".join(str(line) for line in excerpt[-FAILURE_TAIL_LINES:])
        return f"{reason}\n{tail}"
    return reason


def dashboard_serve_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    identify: IdentityProbe | None = None,
    root: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_DASHBOARD_BUILD,),
) -> StepSpec:
    """Start the dashboard server on this install's bundle, and prove it serves."""
    # The console script beside the running interpreter, for an installer
    # invoked by path with no `aq` on PATH -- the same fallback daemon.start has.
    lookup = aq_aware_which(which or shutil.which)
    execute = runner or run_command
    check = probe or http_status
    # A test that injects its own HTTP probe is talking to a fake host; asking
    # the real network who answers behind that fake's back would reach this
    # machine's own dashboard server.
    who = identify or (dashboard_server_identity if probe is None else _nobody)
    path = config_path_for(environ, home)
    log = (home or default_state_dir(environ)) / SERVER_LOG_NAME

    def _look() -> _Observed:
        return _observe(path, probe=check, identify=who, root=root)

    def run(context: StepContext) -> StepResult:
        observed = _look()
        info = observed.info
        detail: dict[str, object] = {"url": info.url, "served": False, "source": info.source}
        if info.source == DASHBOARD_MISCONFIGURED:
            return StepResult.failed(
                STEP_DASHBOARD_SERVE,
                info.hint.split(". Fix it")[0],
                f"Fix dashboard.server in {path}, then rerun the install command.",
                detail=detail,
            )
        if info.source == DASHBOARD_DISABLED:
            return StepResult.succeeded(
                STEP_DASHBOARD_SERVE,
                "dashboard.server.enabled is false, so AQ does not run the dashboard server; "
                "`aq dashboard serve` serves it in the foreground",
                detail=detail,
            )
        if observed.serving:
            return StepResult.succeeded(
                STEP_DASHBOARD_SERVE,
                f"the dashboard server is already serving {info.url}",
                detail={**detail, "served": True},
            )
        if info.source == DASHBOARD_UNBUILT:
            return StepResult.succeeded(STEP_DASHBOARD_SERVE, NO_BUNDLE_HINT, detail=detail)
        if info.source == DASHBOARD_PORT_CONFLICT:
            return StepResult.failed(
                STEP_DASHBOARD_SERVE,
                f"another program answers at {info.url}, not the dashboard server",
                (
                    f"Stop that program, or set dashboard.server.port in {path} to a free "
                    "port; then rerun the install command."
                ),
                detail=detail,
            )
        aq = lookup("aq")
        if not aq:
            return StepResult.failed(
                STEP_DASHBOARD_SERVE,
                "the `aq` command is not on PATH",
                (
                    "Install the AQ runtime (or activate the virtual environment that has it) "
                    "so `aq` resolves, then rerun the install command."
                ),
                detail=detail,
            )
        # Ours but not serving this install's build -- an older build, or a
        # server that answers its identity but not the page -- is restarted;
        # anything else is started.  Both are idempotent.
        action = "restart" if info.source == DASHBOARD_SERVER else "start"
        output = execute([aq, "--json", "dashboard", action], timeout=SERVE_TIMEOUT)
        if output.returncode == EXIT_NO_BUNDLE:
            return StepResult.succeeded(STEP_DASHBOARD_SERVE, NO_BUNDLE_HINT, detail=detail)
        remediation = (
            f"`aq dashboard status` shows the dashboard server's state and {log} its "
            "output. Fix what they name, then rerun the install command (or run "
            "`aq dashboard start`)."
        )
        if not output.ok:
            return StepResult.failed(
                STEP_DASHBOARD_SERVE,
                redact(f"the dashboard server did not start: {_start_failure(output)}"),
                remediation,
                detail={**detail, "log": str(log)},
            )
        after = _look()
        if not after.serving:
            reason = after.info.hint or "it serves an older build than the one installed"
            return StepResult.failed(
                STEP_DASHBOARD_SERVE,
                redact(
                    f"`aq dashboard {action}` finished, but the dashboard is not served at "
                    f"{after.info.url or info.url}: {reason}"
                ),
                remediation,
                detail={**detail, "source": after.info.source, "log": str(log)},
            )
        verb = "restarted" if action == "restart" else "started"
        return StepResult.succeeded(
            STEP_DASHBOARD_SERVE,
            f"{verb} the dashboard server at {after.info.url}",
            detail={**detail, "url": after.info.url, "source": after.info.source,
                    "served": True, "started": True},
        )

    def verify(context: StepContext) -> bool:
        observed = _look()
        if observed.serving:
            return True
        # Nothing to serve, by the operator's choice or because nothing is
        # built; `dashboard.build` fails its own verify in the second case.
        return observed.info.source in (DASHBOARD_DISABLED, DASHBOARD_UNBUILT)

    return StepSpec(
        id=STEP_DASHBOARD_SERVE,
        title="Serve the dashboard",
        description=(
            "Runs `aq dashboard start` and waits until the dashboard server answers with this "
            "install's verified bundle. The dashboard server serves the page and proxies the "
            "daemon's API; the daemon serves no page."
        ),
        run=run,
        depends_on=depends_on,
        # Starting the dashboard server is part of starting AQ in the background.
        capability=CAPABILITY_DAEMON,
        mutating=True,
        consent_prompt="Start the dashboard server now?",
        verify=verify,
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# dashboard.open -- once, interactive only
# ---------------------------------------------------------------------------


def browser_command(
    context: StepContext, which: Callable[[str], str | None]
) -> list[str] | None:
    """The host's own command for opening a URL in the user's browser."""
    facts = context.facts
    if facts.system == "darwin":
        return ["open"]
    if facts.wsl:
        # A WSL distribution has no browser of its own; hand the URL to
        # Windows, whose browsers reach WSL services on localhost.
        for candidate in ("wslview", "explorer.exe"):
            found = which(candidate)
            if found:
                return [found]
        return None
    found = which("xdg-open")
    return [found] if found else None


def dashboard_open_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    identify: IdentityProbe | None = None,
    depends_on: tuple[str, ...] = (STEP_DASHBOARD_SERVE, STEP_DASHBOARD),
) -> StepSpec:
    """Open the dashboard server's URL in a browser, once."""
    lookup = which or shutil.which
    execute = runner or run_command
    check = probe or http_status
    who = identify or (dashboard_server_identity if probe is None else _nobody)
    path = config_path_for(environ, home)

    def run(context: StepContext) -> StepResult:
        if not context.interactive:
            return StepResult.skipped(
                STEP_DASHBOARD_OPEN,
                "an unattended install never opens a browser",
                detail={"url": "", "opened": False},
            )
        # Reachability alone decides; whether a bundle exists only matters
        # for explaining an unreachable dashboard, which is not this step's job.
        info = inspect_dashboard(path, probe=check, identify=who, bundle_present=lambda: True)
        url = info.url
        detail: dict[str, object] = {"url": url, "opened": False}
        if not info.reachable:
            return StepResult.skipped(
                STEP_DASHBOARD_OPEN,
                "the dashboard server is not serving the dashboard, so there is nothing to open",
                detail=detail,
            )
        command = browser_command(context, lookup)
        if command is None:
            return StepResult.succeeded(
                STEP_DASHBOARD_OPEN, f"open {url} in your browser", detail=detail
            )
        output = execute([*command, url], timeout=30)
        # `explorer.exe` exits 1 even after opening the URL, so its exit code
        # is not evidence; any other opener's failure is reported, not fatal.
        opened = output.ok or Path(command[0]).name == "explorer.exe"
        return StepResult.succeeded(
            STEP_DASHBOARD_OPEN,
            f"opened {url} in your browser" if opened else f"open {url} in your browser",
            detail={**detail, "opened": opened},
        )

    return StepSpec(
        id=STEP_DASHBOARD_OPEN,
        title="Open the dashboard",
        description=(
            "Opens the dashboard server's URL in the browser the first time an interactive "
            "install reaches it. Reruns and unattended runs never open a window."
        ),
        run=run,
        depends_on=depends_on,
        # Completed once is enough: a rerun must not pop another window --
        # not even when an earlier install opened the daemon's old /dashboard
        # URL, which the daemon now answers with a pointer to this one.
        verify=lambda context: True,
        owner="onboarding",
    )


__all__ = [
    "BUILD_INPUTS",
    "BUILD_LOG_NAME",
    "NO_BUNDLE_HINT",
    "STAMP_NAME",
    "STEP_DASHBOARD_BUILD",
    "STEP_DASHBOARD_OPEN",
    "STEP_DASHBOARD_SERVE",
    "BuildOutcome",
    "browser_command",
    "build_bundle",
    "bundle_directory",
    "bundle_is_current",
    "bundle_present",
    "dashboard_build_step",
    "dashboard_open_step",
    "dashboard_serve_step",
    "failure_excerpt",
    "installation_root",
    "serves_installed_build",
    "source_checkout_root",
    "source_fingerprint",
    "stamp_path",
]
