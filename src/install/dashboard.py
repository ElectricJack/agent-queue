"""Build the dashboard, have the daemon serve it, and open it once.

A release wheel carries a verified dashboard bundle and the daemon serves it at
``/dashboard``.  A source checkout -- which is what the one-command bootstrap
installs -- carries only the TypeScript, and the installer used to finish by
telling a newcomer to run a Vite dev server by hand.  That is an extra step the
one-command install exists to remove, so these two steps close it:

* ``dashboard.build`` produces the same verified bundle a release ships, using
  the release's own staging script (``scripts/build_release_artifact.py``), and
  restarts a running daemon that is not yet serving it.  The bundle is tied to
  the checkout it was built from, so a rerun after the bootstrap pulled an
  update rebuilds it, and a rerun with nothing new does no work at all.
* ``dashboard.open`` opens the served dashboard in the user's browser -- once,
  on the first interactive install that reaches it, and never from an
  unattended run, which the installation contract forbids from opening one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from .command import CommandOutput, CommandRunner, run_command
from .node_toolchain import Fetcher, ToolchainError, download, ensure_toolchain
from .onboarding import (
    DASHBOARD_PATH,
    STEP_CHECK,
    STEP_DAEMON,
    STEP_DASHBOARD,
    HttpProbe,
    _read_config,
    api_base_url,
    config_path_for,
    http_status,
)
from .redaction import redact
from .results import StepResult
from .state import default_state_dir
from .steps import StepContext, StepSpec

STEP_DASHBOARD_BUILD = "dashboard.build"
STEP_DASHBOARD_OPEN = "dashboard.open"

#: Where each build's full output goes, under AQ's data directory.
BUILD_LOG_NAME = "dashboard-build.log"
#: How many lines of a failed stage's output the step result quotes.
FAILURE_TAIL_LINES = 12

#: `npm ci` downloads the whole workspace on a first run.
NPM_INSTALL_TIMEOUT = 1200.0
#: Type-checking and bundling the dashboard.
BUILD_TIMEOUT = 900.0
#: `aq restart` waits for the daemon's own health check.
RESTART_TIMEOUT = 180.0

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
    """Identify the checkout state the dashboard would be built from.

    The commit plus any uncommitted change to a build input, so both a pulled
    update and a local edit trigger a rebuild.  ``None`` when Git cannot say,
    and then an existing verified bundle is trusted rather than rebuilt forever.
    """
    if not git:
        return None
    head = execute([git, "-C", str(root), "rev-parse", "HEAD"])
    if not head.ok:
        return None
    changes = execute(
        [git, "-C", str(root), "status", "--porcelain", "--", *BUILD_INPUTS]
    )
    if not changes.ok:
        return None
    digest = hashlib.sha256(changes.out.encode("utf-8")).hexdigest()[:16]
    return f"{head.out.strip()}+{digest}"


def _bundle_verifies(root: Path) -> bool:
    from src.dashboard_assets.runtime import verify_dashboard_bundle

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


def dashboard_build_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    root: Path | None = None,
    python: str | None = None,
    fetch: Fetcher = download,
    depends_on: tuple[str, ...] = (STEP_CHECK, STEP_DAEMON),
) -> StepSpec:
    """Build and serve the dashboard from a source checkout."""
    lookup = which or shutil.which
    execute = runner or run_command
    check = probe or http_status
    path = config_path_for(environ, home)
    state_dir = home or default_state_dir(environ)
    interpreter = python or sys.executable

    def _root() -> Path | None:
        return source_checkout_root(root)

    def _fingerprint(checkout: Path) -> str | None:
        return source_fingerprint(checkout, git=lookup("git"), execute=execute)

    def _base() -> str:
        return api_base_url(_read_config(path), environ)

    def _daemon_up() -> bool:
        return check(f"{_base()}/health") in (200, 503)

    def _served() -> bool:
        status = check(f"{_base()}{DASHBOARD_PATH}")
        return status is not None and status < 400

    def _needs_restart() -> bool:
        # The daemon mounts the bundle when it starts, so one that came up
        # before the bundle existed answers 404 until it is restarted.
        return _daemon_up() and not _served()

    def _restart() -> CommandOutput | None:
        aq = lookup("aq")
        if not aq:
            return None
        # `--no-dashboard` keeps `aq restart` from asking about a Vite server:
        # with no terminal behind it that question aborts the command.
        return execute([aq, "restart", "--no-dashboard"], timeout=RESTART_TIMEOUT)

    def _serve(summary: str, detail: dict[str, object]) -> StepResult:
        if not _needs_restart():
            return StepResult.succeeded(STEP_DASHBOARD_BUILD, summary, detail=detail)
        output = _restart()
        if output is None or not output.ok or not _served():
            reason = "`aq` is not on PATH" if output is None else output.message()
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                redact(f"the dashboard is built but the daemon did not start serving it: {reason}"),
                (
                    "Run `aq restart`, then open the dashboard URL; `aq logs` shows why the "
                    "daemon did not come back if it does not."
                ),
                detail=detail,
            )
        return StepResult.succeeded(
            STEP_DASHBOARD_BUILD,
            f"{summary}; restarted the daemon to serve it",
            detail={**detail, "restarted": True},
        )

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
            return _serve("the dashboard is already built for this checkout", detail)

        # The pinned, checksum-verified Node.js AQ owns -- never the machine's.
        try:
            toolchain = ensure_toolchain(
                state_dir, context.facts.system, context.facts.arch, fetch=fetch
            )
        except ToolchainError as error:
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                f"could not provide Node.js for the dashboard build: {error}",
                (
                    "Check this machine can reach https://nodejs.org (a proxy or firewall is "
                    "the usual cause), then rerun the install command; it retries the download."
                ),
                detail=detail,
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
                    interpreter,
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
                return StepResult.failed(
                    STEP_DASHBOARD_BUILD,
                    redact(f"could not {label}:\n{failure_excerpt(output)}"),
                    (
                        f"The full output is in {log}. Fix what it names, then rerun the "
                        "install command; it retries the build."
                    ),
                    detail={**detail, "log": str(log)},
                )

        if not _bundle_verifies(checkout):
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                "the dashboard build finished but its bundle does not verify",
                "Rerun the install command to rebuild it.",
                detail=detail,
            )
        if fingerprint is not None:
            stamp_path(checkout).write_text(fingerprint + "\n", encoding="utf-8")
        return _serve("built the dashboard", {**detail, "built": True})

    def verify(context: StepContext) -> bool:
        checkout = _root()
        if checkout is None:
            return True
        return bundle_is_current(checkout, _fingerprint(checkout)) and not _needs_restart()

    return StepSpec(
        id=STEP_DASHBOARD_BUILD,
        title="Build the dashboard",
        description=(
            "From a source checkout, builds the same verified dashboard bundle a release "
            "ships and restarts a running daemon so it serves it at /dashboard. Rebuilds only "
            "when the checkout changed."
        ),
        run=run,
        depends_on=depends_on,
        mutating=True,
        consent_prompt="Build the dashboard now? (downloads its packages; a few minutes)",
        verify=verify,
        owner="onboarding",
    )


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
    depends_on: tuple[str, ...] = (STEP_DASHBOARD_BUILD, STEP_DASHBOARD),
) -> StepSpec:
    """Open the served dashboard in a browser, once."""
    lookup = which or shutil.which
    execute = runner or run_command
    check = probe or http_status
    path = config_path_for(environ, home)

    def run(context: StepContext) -> StepResult:
        url = f"{api_base_url(_read_config(path), environ)}{DASHBOARD_PATH}/"
        detail: dict[str, object] = {"url": url, "opened": False}
        if not context.interactive:
            return StepResult.skipped(
                STEP_DASHBOARD_OPEN,
                "an unattended install never opens a browser",
                detail=detail,
            )
        status = check(url)
        if status is None or status >= 400:
            return StepResult.skipped(
                STEP_DASHBOARD_OPEN,
                "the daemon is not serving the dashboard, so there is nothing to open",
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
            "Opens the dashboard in the browser the first time an interactive install reaches "
            "it. Reruns and unattended runs never open a window."
        ),
        run=run,
        depends_on=depends_on,
        # Completed once is enough: a rerun must not pop another window.
        verify=lambda context: True,
        owner="onboarding",
    )


__all__ = [
    "BUILD_INPUTS",
    "BUILD_LOG_NAME",
    "STAMP_NAME",
    "STEP_DASHBOARD_BUILD",
    "STEP_DASHBOARD_OPEN",
    "browser_command",
    "bundle_directory",
    "bundle_is_current",
    "dashboard_build_step",
    "dashboard_open_step",
    "failure_excerpt",
    "source_checkout_root",
    "source_fingerprint",
    "stamp_path",
]
