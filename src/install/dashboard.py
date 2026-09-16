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
from .steps import StepContext, StepSpec

STEP_DASHBOARD_BUILD = "dashboard.build"
STEP_DASHBOARD_OPEN = "dashboard.open"

#: The oldest Node.js the dashboard toolchain (Vite 6, openapi-ts) supports.
MIN_NODE_MAJOR = 18

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


def node_major(node: str, execute: CommandRunner) -> int | None:
    output = execute([node, "--version"])
    if not output.ok:
        return None
    text = output.out.strip().lstrip("v")
    try:
        return int(text.split(".", 1)[0])
    except ValueError:
        return None


def _node_remediation(context: StepContext) -> str:
    if context.facts.system == "darwin":
        install = "`brew install node`"
    else:
        install = "`sudo apt-get install -y nodejs npm`"
    return (
        f"Install Node.js {MIN_NODE_MAJOR} or newer ({install}), then rerun the install "
        "command; it builds the dashboard and continues."
    )


def dashboard_build_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    root: Path | None = None,
    python: str | None = None,
    depends_on: tuple[str, ...] = (STEP_CHECK, STEP_DAEMON),
) -> StepSpec:
    """Build and serve the dashboard from a source checkout."""
    lookup = which or shutil.which
    execute = runner or run_command
    check = probe or http_status
    path = config_path_for(environ, home)
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

        npm = lookup("npm")
        node = lookup("node")
        if not npm or not node:
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                "Node.js is not installed, so the dashboard cannot be built",
                _node_remediation(context),
            )
        major = node_major(node, execute)
        if major is None or major < MIN_NODE_MAJOR:
            found = "an unreadable version" if major is None else f"Node.js {major}"
            return StepResult.failed(
                STEP_DASHBOARD_BUILD,
                f"the dashboard needs Node.js {MIN_NODE_MAJOR}+, but {node} is {found}",
                _node_remediation(context),
            )

        # A Node.js this run just installed (Homebrew) is not on this process's
        # PATH yet, and npm's scripts look `node` up by name.
        env = dict(os.environ if environ is None else environ)
        env["PATH"] = os.pathsep.join(
            dict.fromkeys([str(Path(npm).parent), str(Path(node).parent), env.get("PATH", "")])
        )
        stages = (
            (
                "install the dashboard's packages",
                [npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"],
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
        for label, argv, timeout in stages:
            output = execute(argv, timeout=timeout, env=env, cwd=str(checkout))
            if not output.ok:
                return StepResult.failed(
                    STEP_DASHBOARD_BUILD,
                    redact(f"could not {label}: {output.message()}"),
                    (
                        "Fix what the message names (a network failure is the usual cause), "
                        "then rerun the install command; it retries the build."
                    ),
                    detail=detail,
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
    "MIN_NODE_MAJOR",
    "STAMP_NAME",
    "STEP_DASHBOARD_BUILD",
    "STEP_DASHBOARD_OPEN",
    "browser_command",
    "bundle_directory",
    "bundle_is_current",
    "dashboard_build_step",
    "dashboard_open_step",
    "node_major",
    "source_checkout_root",
    "source_fingerprint",
    "stamp_path",
]
