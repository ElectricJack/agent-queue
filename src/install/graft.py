"""graft: an optional, recommended code graph, set up the AQ way.

graft (``@nanonets/graft`` on npm) builds a code graph of one repository and
gives Claude Code MCP tools and hooks that answer from it, so an agent reads
fewer files.  ``aq install`` offers it; when the operator accepts, two steps
set it up the way the fleet runs it (the operator amendment to the
project-defaults design, 2026-09-27):

* **Pinned, never upgraded.**  A missing graft is installed from the cleared
  series (:data:`GRAFT_SERIES`).  A present one is reused whatever its
  version; a version outside the series is reported as drift.  An ``npm i -g``
  is what replaced the worker guards last time, so AQ never runs one on a graft
  that is already there.
* **Telemetry off.**  ``graft telemetry disable`` records the choice in
  graft's own state file, which an npm upgrade does not touch, and
  ``DO_NOT_TRACK=1`` is set wherever AQ can put it without a shim: on every
  graft process the installer starts, and in each main checkout's
  ``.claude/settings.local.json`` ``env``, which Claude Code hands to graft's
  hooks and MCP server.  graft rewrites its ``.mcp.json`` entry and its hook
  shims on every refresh, so neither can carry it.
* **Per repository, never global.**  ``graft init --no-global
  --no-statusline --no-agents`` runs in each registered project's main
  checkout.  AQ never writes ``~/.claude/settings.json``, ``~/.claude.json``,
  ``~/.codex`` or the OpenCode configuration; graft wiring found there is
  reported, not removed.  ``--no-agents`` keeps it to Claude Code's wiring:
  without an agent list graft writes nothing on a pipe, and every other agent's
  wiring is an instruction file (``AGENTS.md``) that worker slots would read.
* **Off in worker worktrees.**  Slots, linked worktrees, job snapshots and the
  vault are never wired.  Worker sessions carry ``GRAFT_NO_SEED``,
  ``GRAFT_NO_REFRESH`` and ``DO_NOT_TRACK`` in their launch environment
  (:data:`src.sessions.spec.WORKER_TOOL_ENV`), and the installer writes no
  shim an npm upgrade would replace.
* **Reruns report; they do not re-wire.**  A checkout graft already wired is
  never initialised again, and what differs from the AQ way is reported as
  drift.  The installer's own writes are all local to the machine: the
  repository's ``info/exclude`` and ``.claude/settings.local.json``, never a
  tracked file.  Committing ``/graft/`` to ``.gitignore`` belongs to the
  project's own delivery (the project-defaults chore).

Both steps are advisory: graft is optional, so a failure is reported with its
remediation and never changes the install's outcome or exit code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .command import DEFAULT_TIMEOUT, INSTALL_TIMEOUT, CommandRunner, run_command
from .prerequisites import STEP_HOST
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec

CAPABILITY_GRAFT = "graft"
STEP_GRAFT_CLI = "graft.cli"
STEP_GRAFT_REPOS = "graft.repos"

GRAFT_EXECUTABLE = "graft"
GRAFT_PACKAGE = "@nanonets/graft"
#: The release series cleared for AQ.  Raise it only once the worker guards
#: (``WORKER_TOOL_ENV``, the hook helper's early exit) are re-checked against
#: the new version.
GRAFT_SERIES = "0.18"
GRAFT_INSTALL_SPEC = f"{GRAFT_PACKAGE}@{GRAFT_SERIES}.x"
GRAFT_INIT_ARGS: tuple[str, ...] = ("init", "--no-global", "--no-statusline", "--no-agents")
#: Set on every graft process the installer starts.
GRAFT_ENV: Mapping[str, str] = {"DO_NOT_TRACK": "1"}
#: What graft is, in the words the wizard and the summary use.
GRAFT_PITCH = "a per-repo code graph whose MCP tools and hooks cut file reads"

#: Ignore patterns the installer adds to a checkout's ``info/exclude`` when
#: nothing ignores the path yet: graft's regenerable index, and the local
#: settings file that carries ``DO_NOT_TRACK``.
GRAFT_CACHE = "graft/"
LOCAL_SETTINGS = ".claude/settings.local.json"

#: Paths under a repository that mark it as already wired by graft.
_WIRED_MARKERS = (
    Path(".claude") / "helpers" / "graft-hooks.cjs",
    Path("graft") / ".cache" / "wiring-stamp.json",
)
_STAMP = Path("graft") / ".cache" / "wiring-stamp.json"
_VERSION = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\b")
_GRAFT_KEY = re.compile(r'"graft"\s*:')

CommandLookup = Callable[[str], str | None]
#: Returns every workspace row the daemon knows, as ``list_workspaces`` reports
#: them, or raises :class:`CheckoutListError`.
WorkspaceLister = Callable[[], Sequence[Mapping[str, Any]]]


class CheckoutListError(Exception):
    """The daemon could not be asked which projects exist."""


# ---------------------------------------------------------------------------
# The graft CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraftCli:
    """What ``graft --version`` said, or that there is no runnable graft."""

    path: str | None
    version: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def cleared(self) -> bool:
        return version_cleared(self.version)


def version_cleared(version: str | None) -> bool:
    """True when *version* belongs to the series cleared for AQ."""
    match = _VERSION.search(version or "")
    return bool(match) and f"{match.group(1)}.{match.group(2)}" == GRAFT_SERIES


def graft_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every graft process the installer starts runs with."""
    return {**(os.environ if environ is None else environ), **GRAFT_ENV}


def probe_graft(
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = run_command,
    environ: Mapping[str, str] | None = None,
) -> GraftCli:
    """Find graft and ask its version; a graft that cannot answer is absent."""
    path = which(GRAFT_EXECUTABLE)
    if not path:
        return GraftCli(path=None)
    output = runner((path, "--version"), timeout=DEFAULT_TIMEOUT, env=graft_env(environ))
    if not output.ok:
        return GraftCli(path=None)
    # Only a version token is kept: whatever else a PATH executable prints
    # never reaches the resume record.
    match = _VERSION.search(output.stdout or "")
    return GraftCli(path=path, version=match.group(0) if match else "unknown")


def telemetry_disabled(home: Path) -> bool:
    """Whether graft's own state file records telemetry as turned off."""
    try:
        state = json.loads((home / ".graft" / "telemetry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and state.get("enabled") is False


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def global_wiring(home: Path) -> tuple[str, ...]:
    """Graft wiring outside any repository, which AQ reports and never edits.

    Each of these reaches every session on the machine -- the vault's
    supervisor sessions and worker slots included -- which is what the
    per-repository policy exists to prevent.
    """
    found: list[str] = []
    settings = _text(home / ".claude" / "settings.json")
    if "graft-hooks" in settings or "graft-statusline" in settings:
        found.append("~/.claude/settings.json runs graft's hooks in every Claude Code session")
    try:
        user_config = json.loads(_text(home / ".claude.json") or "{}")
    except ValueError:
        user_config = {}
    servers = user_config.get("mcpServers") if isinstance(user_config, dict) else None
    if isinstance(servers, dict) and GRAFT_EXECUTABLE in servers:
        found.append("~/.claude.json registers graft's MCP server for every Claude Code session")
    if "[mcp_servers.graft]" in _text(home / ".codex" / "config.toml"):
        found.append("~/.codex/config.toml registers graft's MCP server for every Codex session")
    if GRAFT_EXECUTABLE in _text(home / ".codex" / "hooks.json"):
        found.append("~/.codex/hooks.json runs graft's hooks in every Codex session")
    for relative in (
        Path(".config") / "opencode" / "opencode.json",
        Path(".config") / "opencode" / "opencode.jsonc",
        Path(".gemini") / "config" / "mcp_config.json",
        Path(".gemini") / "settings.json",
    ):
        if _GRAFT_KEY.search(_text(home / relative)):
            found.append(f"~/{relative.as_posix()} registers graft's MCP server machine-wide")
    return tuple(found)


def _cli_drift(cli: GraftCli, home: Path) -> list[str]:
    drift: list[str] = []
    if cli.available and not cli.cleared:
        drift.append(
            f"graft {cli.version} is outside the {GRAFT_SERIES}.x series cleared for AQ. "
            "AQ never upgrades or downgrades graft; after a version change, check that worker "
            "sessions still skip graft (GRAFT_NO_SEED / GRAFT_NO_REFRESH)."
        )
    drift.extend(
        f"{finding} — AQ wires graft per repository only; remove it by hand."
        for finding in global_wiring(home)
    )
    return drift


def graft_cli_step(
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = run_command,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> StepSpec:
    """Install graft from the cleared series if it is missing, and turn telemetry off."""

    def user_home() -> Path:
        return home or Path.home()

    def observe() -> GraftCli:
        return probe_graft(which=which, runner=runner, environ=environ)

    def detail(cli: GraftCli, *, installed: bool) -> dict[str, Any]:
        return {
            "executable": cli.path,
            "version": cli.version,
            "series": f"{GRAFT_SERIES}.x",
            "cleared": cli.cleared,
            "installed": installed,
            "telemetry": "disabled",
            "drift": _cli_drift(cli, user_home()),
        }

    def summary(cli: GraftCli, *, installed: bool, drift: list[str]) -> str:
        verb = "installed" if installed else "reusing"
        text = f"{verb} graft {cli.version} at {cli.path}; telemetry off"
        return text + (f"; {len(drift)} drift finding(s) reported" if drift else "")

    def run(context: StepContext) -> StepResult:
        del context
        cli = observe()
        installed = False
        if not cli.available:
            npm = which("npm")
            if npm is None:
                return StepResult.failed(
                    STEP_GRAFT_CLI,
                    "graft is not installed and npm is not on PATH to install it",
                    "Install Node.js 20 or newer with npm (https://nodejs.org), then rerun "
                    f"`aq install --with {CAPABILITY_GRAFT}`.",
                )
            command = (npm, "install", "-g", GRAFT_INSTALL_SPEC)
            output = runner(command, timeout=INSTALL_TIMEOUT, env=graft_env(environ))
            if not output.ok:
                return StepResult.failed(
                    STEP_GRAFT_CLI,
                    f"`npm install -g {GRAFT_INSTALL_SPEC}` failed: {output.message()}",
                    f"Run `npm install -g {GRAFT_INSTALL_SPEC}` yourself to see npm's output "
                    "(a global prefix owned by root needs `npm config set prefix ~/.local`), "
                    f"then rerun `aq install --with {CAPABILITY_GRAFT}`.",
                )
            cli = observe()
            if not cli.available:
                return StepResult.failed(
                    STEP_GRAFT_CLI,
                    "npm installed graft, but no runnable `graft` is on PATH",
                    "Put npm's global bin directory (`npm prefix -g`, then /bin) on your PATH, "
                    f"open a new shell and rerun `aq install --with {CAPABILITY_GRAFT}`.",
                )
            installed = True
        if not telemetry_disabled(user_home()):
            output = runner(
                (cli.path, "telemetry", "disable"),
                timeout=DEFAULT_TIMEOUT,
                env=graft_env(environ),
            )
            if not output.ok or not telemetry_disabled(user_home()):
                return StepResult.failed(
                    STEP_GRAFT_CLI,
                    f"`graft telemetry disable` did not take effect: {output.message()}",
                    "Run `graft telemetry disable` yourself, then rerun "
                    f"`aq install --with {CAPABILITY_GRAFT}`.",
                    detail={"executable": cli.path, "version": cli.version},
                )
        facts = detail(cli, installed=installed)
        return StepResult.succeeded(
            STEP_GRAFT_CLI,
            summary(cli, installed=installed, drift=facts["drift"]),
            detail=facts,
            resources=(
                ResourceRecord(
                    kind="npm-package",
                    id=GRAFT_PACKAGE,
                    owned=installed,
                    reused=not installed,
                    detail={"executable": cli.path, "version": cli.version},
                ),
            ),
        )

    def verify(context: StepContext) -> bool | StepResult:
        del context
        cli = observe()
        if not cli.available or not telemetry_disabled(user_home()):
            # Gone, or telemetry back on: the step runs again, which reuses a
            # present graft and never upgrades it.
            return False
        facts = detail(cli, installed=False)
        return StepResult.succeeded(
            STEP_GRAFT_CLI,
            summary(cli, installed=False, drift=facts["drift"]),
            detail=facts,
        )

    return StepSpec(
        id=STEP_GRAFT_CLI,
        title="Install or reuse graft",
        description=(
            f"graft is {GRAFT_PITCH}. Reuses an installed graft and never upgrades it; when "
            f"missing, installs {GRAFT_INSTALL_SPEC} with npm. Turns graft's telemetry off and "
            "reports graft wiring found outside any repository without changing it."
        ),
        run=run,
        depends_on=(STEP_HOST,),
        capability=CAPABILITY_GRAFT,
        mutating=True,
        advisory=True,
        consent_prompt=(
            f"Install graft ({GRAFT_INSTALL_SPEC}) with npm if it is missing, and turn its "
            "telemetry off?"
        ),
        verify=verify,
        owner="graft",
    )


# ---------------------------------------------------------------------------
# Main checkouts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Checkout:
    """One registered project's main checkout."""

    project_id: str
    path: Path


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _in_worker_worktree(path: Path) -> bool:
    parts = path.parts
    return any(parts[i : i + 2] == (".aq", "worktrees") for i in range(len(parts) - 1))


def _skip_reason(row: Mapping[str, Any], path: Path, vault: Path) -> str | None:
    """Why a base workspace is not a checkout graft may be set up in, if it is not."""
    if _within(path, vault):
        return "inside the AQ vault, which graft never indexes"
    if _in_worker_worktree(path):
        return "a worker worktree, where graft stays off"
    if row.get("enabled") is False:
        return "the workspace is disabled"
    if not path.is_dir():
        return "the directory does not exist"
    if (path / ".git").is_file():
        return "a linked git worktree, not a main checkout"
    if not (path / ".git").is_dir():
        return "not a git repository"
    return None


def main_checkouts(
    rows: Sequence[Mapping[str, Any]], *, vault: Path
) -> tuple[tuple[Checkout, ...], tuple[dict[str, str], ...]]:
    """Pick each project's main checkouts out of the daemon's workspace rows.

    A main checkout is a base workspace: a non-slot row whose kind runs in
    worktree mode, so no agent ever works in it.  Slots, job snapshots and
    the vault's own rows are not candidates at all.  A base that is inside
    the vault, disabled, missing, not a repository, or itself a linked git
    worktree is returned as skipped, with the reason.
    """
    checkouts: list[Checkout] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()
    vault = vault.expanduser()
    for row in rows:
        if row.get("role") == "slot" or row.get("mode") != "worktree":
            continue
        raw = str(row.get("workspace_path") or "")
        path = Path(raw).expanduser()
        if not raw or path in seen:
            continue
        seen.add(path)
        project = str(row.get("project_id") or "?")
        reason = _skip_reason(row, path, vault)
        if reason is None:
            checkouts.append(Checkout(project_id=project, path=path))
        else:
            skipped.append({"project_id": project, "path": str(path), "reason": reason})
    return tuple(checkouts), tuple(skipped)


def _execute(
    api_url: str,
    command: str,
    args: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """One CommandHandler command through the daemon's ``/api/execute``."""
    headers = {"Content-Type": "application/json"}
    token = (os.environ if environ is None else environ).get("AQ_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/execute",
        data=json.dumps({"command": command, "args": dict(args)}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (OSError, ValueError):
            raise CheckoutListError(f"{command} answered HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise CheckoutListError(f"no daemon answered at {api_url} ({error})") from error
    if not isinstance(payload, dict) or not payload.get("ok"):
        message = payload.get("error") if isinstance(payload, dict) else None
        raise CheckoutListError(f"{command} failed: {message or 'unexpected response'}")
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def daemon_workspaces(
    api_url: str, *, environ: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every project's workspace rows, as the daemon's ``list_workspaces`` reports them.

    Asked per project: ``list_workspaces`` without a project narrows to the
    handler's active project when one is set.
    """
    projects = _execute(api_url, "list_projects", {}, environ=environ).get("projects") or []
    rows: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict) or not project.get("id"):
            continue
        listed = _execute(
            api_url, "list_workspaces", {"project_id": project["id"]}, environ=environ
        ).get("workspaces")
        rows.extend(row for row in listed or () if isinstance(row, dict))
    return rows


# ---------------------------------------------------------------------------
# One checkout
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CheckoutState:
    """What one main checkout looks like, against the AQ way."""

    wired: bool
    cache_ignored: bool | None
    settings_ignored: bool | None
    #: ``True``/``False`` for ``env.DO_NOT_TRACK``; ``None`` when the local
    #: settings file is not a JSON object, which AQ never overwrites.
    do_not_track: bool | None
    drift: list[str] = field(default_factory=list)

    @property
    def needs_work(self) -> bool:
        return (
            not self.wired
            or self.cache_ignored is False
            or self.do_not_track is False
            or (self.do_not_track is True and self.settings_ignored is False)
        )


def _json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object in *path*: ``{}`` when absent, ``None`` when not an object."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _wired(repo: Path) -> bool:
    """Whether graft is already wired here, by ``graft init`` or by a committed template."""
    if any((repo / marker).exists() for marker in _WIRED_MARKERS):
        return True
    if "graft-hooks" in _text(repo / ".claude" / "settings.json"):
        return True
    mcp = _json_object(repo / ".mcp.json") or {}
    servers = mcp.get("mcpServers")
    return isinstance(servers, dict) and GRAFT_EXECUTABLE in servers


def _stamp_drift(repo: Path) -> list[str]:
    stamp = _json_object(repo / _STAMP) or {}
    opts = stamp.get("opts") if isinstance(stamp.get("opts"), dict) else {}
    drift: list[str] = []
    if opts.get("global") is True:
        drift.append(
            f"{repo}: graft's wiring stamp says global: true, so graft's own upkeep rewrites "
            f'~/.claude and ~/.codex; set "global": false in {repo / _STAMP}.'
        )
    if opts.get("statusline") is True:
        drift.append(
            f"{repo}: graft was wired with its statusLine; AQ wires it with --no-statusline."
        )
    return drift


def _ignored(runner: CommandRunner, repo: Path, relative: str) -> bool | None:
    output = runner(
        ("git", "check-ignore", "-q", "--no-index", "--", relative),
        cwd=str(repo),
        timeout=DEFAULT_TIMEOUT,
    )
    if output.returncode == 0:
        return True
    if output.returncode == 1:
        return False
    return None


def inspect_checkout(repo: Path, *, runner: CommandRunner = run_command) -> CheckoutState:
    """Observe *repo* without changing anything."""
    local = _json_object(repo / LOCAL_SETTINGS)
    env = local.get("env") if local is not None else None
    do_not_track = None if local is None else isinstance(env, dict) and env.get("DO_NOT_TRACK") == "1"
    state = CheckoutState(
        wired=_wired(repo),
        cache_ignored=_ignored(runner, repo, GRAFT_CACHE),
        settings_ignored=_ignored(runner, repo, LOCAL_SETTINGS),
        do_not_track=do_not_track,
        drift=_stamp_drift(repo),
    )
    if local is None:
        state.drift.append(
            f"{repo}: {LOCAL_SETTINGS} is not a JSON object, so AQ could not set "
            'env.DO_NOT_TRACK="1" there; fix the file and rerun `aq install --with graft`.'
        )
    return state


def _exclude(runner: CommandRunner, repo: Path, relative: str, why: str) -> bool:
    """Ignore *relative* in *repo* through ``info/exclude``: local, never a tracked edit."""
    output = runner(
        ("git", "rev-parse", "--git-path", "info/exclude"),
        cwd=str(repo),
        timeout=DEFAULT_TIMEOUT,
    )
    if not output.ok or not output.out:
        return False
    path = Path(output.out)
    path = path if path.is_absolute() else repo / path
    pattern = f"/{relative}"
    existing = _text(path)
    if pattern not in existing.splitlines():
        gap = "" if not existing or existing.endswith("\n") else "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{existing}{gap}# aq install: {why}\n{pattern}\n", encoding="utf-8")
    return _ignored(runner, repo, relative) is True


def _set_do_not_track(repo: Path) -> None:
    path = repo / LOCAL_SETTINGS
    settings = _json_object(path)
    if settings is None:  # pragma: no cover - the caller checked
        return
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    settings["env"] = {**env, **GRAFT_ENV}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def set_up_checkout(
    repo: Path,
    graft: str,
    *,
    runner: CommandRunner = run_command,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bring one main checkout to the AQ way; never re-wire what graft already wired."""
    state = inspect_checkout(repo, runner=runner)
    row: dict[str, Any] = {"path": str(repo), "changes": []}
    if state.wired:
        row["status"] = "already_set_up"
    else:
        output = runner(
            (graft, *GRAFT_INIT_ARGS),
            cwd=str(repo),
            timeout=INSTALL_TIMEOUT,
            env=graft_env(environ),
        )
        if not output.ok:
            row.update(status="failed", reason=f"graft init failed: {output.message()}")
            return row
        state = inspect_checkout(repo, runner=runner)
        if not state.wired:
            # What graft does on a pipe when it is not told which agents to wire.
            row.update(
                status="failed",
                reason=f"graft init exited 0 but wired nothing: {output.message()}",
            )
            return row
        row["status"] = "set_up"
        row["changes"].append("ran `graft " + " ".join(GRAFT_INIT_ARGS) + "`")
    if state.cache_ignored is False:
        if _exclude(runner, repo, GRAFT_CACHE, "graft's regenerable index"):
            row["changes"].append(f"ignored {GRAFT_CACHE} in the repository's info/exclude")
        else:
            state.drift.append(f"{repo}: {GRAFT_CACHE} is still not ignored by git.")
    if state.do_not_track is not None and state.settings_ignored is False:
        # Ignored first, so the file AQ writes can never show up as a change.
        if _exclude(runner, repo, LOCAL_SETTINGS, "machine-local Claude Code settings"):
            row["changes"].append(f"ignored {LOCAL_SETTINGS} in the repository's info/exclude")
            state.settings_ignored = True
        else:
            state.drift.append(
                f"{repo}: {LOCAL_SETTINGS} is not ignored by git, so AQ did not write "
                "DO_NOT_TRACK there."
            )
    if state.do_not_track is False and state.settings_ignored is not False:
        _set_do_not_track(repo)
        row["changes"].append(f'set env.DO_NOT_TRACK="1" in {LOCAL_SETTINGS}')
    row["drift"] = state.drift
    return row


def graft_repos_step(
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = run_command,
    list_workspaces: WorkspaceLister,
    vault: Path,
    environ: Mapping[str, str] | None = None,
    depends_on: tuple[str, ...] = (STEP_HOST,),
) -> StepSpec:
    """Set graft up in each registered project's main checkout, and nowhere else."""

    def listed() -> tuple[tuple[Checkout, ...], tuple[dict[str, str], ...]]:
        return main_checkouts(list_workspaces(), vault=vault)

    def unavailable(error: CheckoutListError) -> StepResult:
        return StepResult.failed(
            STEP_GRAFT_REPOS,
            f"could not list the registered projects: {error}",
            f"Start AQ with `aq start`, then rerun `aq install --with {CAPABILITY_GRAFT}`.",
        )

    def report(
        rows: list[dict[str, Any]], skipped: tuple[dict[str, str], ...]
    ) -> StepResult:
        drift = [line for row in rows for line in row.get("drift") or ()]
        facts = {"checkouts": rows, "skipped": list(skipped), "drift": drift}
        failed = [row for row in rows if row.get("status") == "failed"]
        if failed:
            first = failed[0]["path"]
            return StepResult.failed(
                STEP_GRAFT_REPOS,
                f"graft init failed in {len(failed)} of {len(rows)} main checkout(s): "
                + "; ".join(f"{row['path']}: {row['reason']}" for row in failed),
                f"Run `graft {' '.join(GRAFT_INIT_ARGS)}` in {first} to see graft's own "
                f"output, then rerun `aq install --with {CAPABILITY_GRAFT}`.",
                detail=facts,
            )
        new = sum(1 for row in rows if row.get("status") == "set_up")
        if not rows:
            text = "no registered project has a main checkout to set up yet"
        else:
            text = f"graft is set up in {len(rows)} main checkout(s), {new} of them by this run"
        return StepResult.succeeded(
            STEP_GRAFT_REPOS,
            text + (f"; {len(drift)} drift finding(s) reported" if drift else ""),
            detail=facts,
        )

    def run(context: StepContext) -> StepResult:
        del context
        graft = which(GRAFT_EXECUTABLE)
        if graft is None:
            return StepResult.skipped(
                STEP_GRAFT_REPOS,
                f"graft is not installed, so no checkout was set up ({STEP_GRAFT_CLI} "
                "installs it)",
            )
        try:
            checkouts, skipped = listed()
        except CheckoutListError as error:
            return unavailable(error)
        rows = []
        for checkout in checkouts:
            row = set_up_checkout(checkout.path, graft, runner=runner, environ=environ)
            rows.append({"project_id": checkout.project_id, **row})
        return report(rows, skipped)

    def verify(context: StepContext) -> bool | StepResult:
        del context
        if which(GRAFT_EXECUTABLE) is None:
            return False
        try:
            checkouts, skipped = listed()
        except CheckoutListError:
            return False
        rows = []
        for checkout in checkouts:
            state = inspect_checkout(checkout.path, runner=runner)
            if state.needs_work:
                # A new project, or a setting that went missing: run again,
                # which sets up only what is missing and re-wires nothing.
                return False
            rows.append(
                {
                    "project_id": checkout.project_id,
                    "path": str(checkout.path),
                    "status": "already_set_up",
                    "changes": [],
                    "drift": state.drift,
                }
            )
        return report(rows, skipped)

    return StepSpec(
        id=STEP_GRAFT_REPOS,
        title="Set graft up in each project's main checkout",
        description=(
            f"Runs `graft {' '.join(GRAFT_INIT_ARGS)}` in each registered project's main "
            "checkout that graft has not wired yet, ignores graft/ there and sets "
            "DO_NOT_TRACK=1 in its local Claude Code settings. Never touches worker "
            "worktrees, the vault or any machine-wide configuration."
        ),
        run=run,
        depends_on=depends_on,
        capability=CAPABILITY_GRAFT,
        mutating=True,
        advisory=True,
        consent_prompt=(
            "Set graft up in each registered project's main checkout (repository-level "
            "only, never global)?"
        ),
        verify=verify,
        owner="graft",
    )


def graft_steps(
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = run_command,
    list_workspaces: WorkspaceLister,
    vault: Path,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    repos_after: tuple[str, ...] = (),
) -> tuple[StepSpec, ...]:
    """Both graft steps.  *repos_after* is what the checkout step waits for (the daemon)."""
    return (
        graft_cli_step(which=which, runner=runner, home=home, environ=environ),
        graft_repos_step(
            which=which,
            runner=runner,
            list_workspaces=list_workspaces,
            vault=vault,
            environ=environ,
            depends_on=(STEP_HOST, *repos_after),
        ),
    )


__all__ = [
    "CAPABILITY_GRAFT",
    "GRAFT_ENV",
    "GRAFT_INIT_ARGS",
    "GRAFT_INSTALL_SPEC",
    "GRAFT_PACKAGE",
    "GRAFT_PITCH",
    "GRAFT_SERIES",
    "STEP_GRAFT_CLI",
    "STEP_GRAFT_REPOS",
    "Checkout",
    "CheckoutListError",
    "CheckoutState",
    "GraftCli",
    "daemon_workspaces",
    "global_wiring",
    "graft_cli_step",
    "graft_env",
    "graft_repos_step",
    "graft_steps",
    "inspect_checkout",
    "main_checkouts",
    "probe_graft",
    "set_up_checkout",
    "telemetry_disabled",
    "version_cleared",
]
