"""graft in ``aq install``: offered, pinned, per repository, never global.

The steps shell out to ``graft``, ``npm`` and ``git``.  ``graft`` and ``npm``
are faked here -- a test must not install a global npm package or depend on
graft being on the box -- while ``git`` runs for real against throwaway
repositories with the machine's own git configuration switched off, so an
operator's global excludes file cannot decide what counts as ignored.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.install.command import CommandOutput, run_command
from src.install.engine import InstallEngine, InstallOptions
from src.install.graft import (
    CAPABILITY_GRAFT,
    GRAFT_INIT_ARGS,
    GRAFT_INSTALL_SPEC,
    STEP_GRAFT_CLI,
    STEP_GRAFT_REPOS,
    CheckoutListError,
    GraftCli,
    daemon_workspaces,
    global_wiring,
    graft_cli_step,
    graft_repos_step,
    graft_steps,
    main_checkouts,
    probe_graft,
    version_cleared,
)
from src.install.platform import TIER_SUPPORTED, PlatformFacts, SupportVerdict
from src.install.prerequisites import STEP_HOST
from src.install.results import InstallOutcome, StepResult, StepState
from src.install.steps import StepRegistry, StepSpec

GRAFT = "/opt/graft/bin/graft"
NPM = "/usr/bin/npm"


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists."""


def _support() -> SupportVerdict:
    facts = PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        wsl=True,
        wsl_version=2,
    )
    return SupportVerdict(host_path="windows-wsl2", tier=TIER_SUPPORTED, facts=facts)


def _git_env(home: Path) -> dict[str, str]:
    """git with no global or system configuration: only the repository decides."""
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


class Host:
    """graft and npm as the steps see them, git for real."""

    def __init__(
        self,
        home: Path,
        *,
        installed: bool = True,
        version: str = "0.18.0",
        npm: bool = True,
        npm_ok: bool = True,
        init_ok: bool = True,
        init_wires: bool = True,
        init_ignores_cache: bool = True,
    ) -> None:
        self.home = home
        self.installed = installed
        self.version = version
        self.npm = npm
        self.npm_ok = npm_ok
        self.init_ok = init_ok
        self.init_wires = init_wires
        self.init_ignores_cache = init_ignores_cache
        self.calls: list[tuple[tuple[str, ...], dict[str, str] | None, str | None]] = []

    def which(self, name: str) -> str | None:
        if name == "graft":
            return GRAFT if self.installed else None
        if name == "npm":
            return NPM if self.npm else None
        return None

    def commands(self, program: str) -> list[tuple[str, ...]]:
        return [argv for argv, _env, _cwd in self.calls if argv[0] == program]

    def run(self, argv, *, timeout=None, env=None, input_text=None, cwd=None) -> CommandOutput:
        argv = tuple(str(part) for part in argv)
        self.calls.append((argv, None if env is None else dict(env), cwd))
        if argv[0] == "git":
            return run_command(argv, timeout=timeout, env=_git_env(self.home), cwd=cwd)
        if argv[0] == NPM:
            if not self.npm_ok:
                return CommandOutput(argv, 243, "", "npm ERR! code EACCES")
            self.installed = True
            return CommandOutput(argv, 0, "added 1 package")
        if argv[0] == GRAFT:
            return self._graft(argv, cwd)
        raise AssertionError(f"unexpected command: {argv}")

    def _graft(self, argv: tuple[str, ...], cwd: str | None) -> CommandOutput:
        sub = argv[1:]
        if sub == ("--version",):
            return CommandOutput(argv, 0, f"{self.version}\n")
        if sub == ("telemetry", "disable"):
            state = self.home / ".graft" / "telemetry.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"enabled": False}), encoding="utf-8")
            return CommandOutput(argv, 0, "telemetry: off.")
        if sub[:1] == ("init",):
            if not self.init_ok:
                return CommandOutput(argv, 1, "", "✗ could not build the graph")
            if not self.init_wires:
                # graft on a pipe with no agent list: help text, exit 0, no files.
                return CommandOutput(argv, 0, "", "graft init: pass --agents <ids> or --yes")
            repo = Path(cwd or ".")
            helper = repo / ".claude" / "helpers" / "graft-hooks.cjs"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("// hooks shim\n", encoding="utf-8")
            stamp = repo / "graft" / ".cache" / "wiring-stamp.json"
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(
                json.dumps({"version": self.version, "opts": {"global": False, "statusline": False}}),
                encoding="utf-8",
            )
            if self.init_ignores_cache:
                with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
                    handle.write("/graft/\n")
            return CommandOutput(argv, 0, "✓ wrote .claude/settings.json")
        raise AssertionError(f"unexpected graft command: {argv}")


def _repo(path: Path, home: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(path)), check=True, env=_git_env(home))
    return path


def _row(path: Path, project: str = "alpha", **overrides) -> dict:
    return {
        "project_id": project,
        "workspace_path": str(path),
        "role": "base",
        "mode": "worktree",
        "enabled": True,
        **overrides,
    }


def _telemetry_off(home: Path) -> None:
    state = home / ".graft" / "telemetry.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"enabled": False}), encoding="utf-8")


def _registry(host: Host, rows, *, vault: Path) -> StepRegistry:
    def listed():
        if isinstance(rows, Exception):
            raise rows
        return rows

    started = StepSpec(
        id=STEP_HOST,
        title="host",
        run=lambda context: StepResult.succeeded(STEP_HOST, "supported"),
    )
    return StepRegistry(
        (
            started,
            *graft_steps(
                which=host.which,
                runner=host.run,
                list_workspaces=listed,
                vault=vault,
                home=host.home,
            ),
        )
    )


def _install(registry: StepRegistry, state: Path, **options):
    options.setdefault("capabilities", frozenset({CAPABILITY_GRAFT}))
    options.setdefault("approve", frozenset({"*"}))
    options.setdefault("interactive", False)
    return InstallEngine(
        registry, InstallOptions(state_path=state, **options), support=_support()
    ).run()


def _step(result, step_id):
    return next(row for row in result.steps if row.step_id == step_id)


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


# -- detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "cleared"),
    [("0.18.0", True), ("0.18.7", True), ("v0.18.2", True), ("0.20.0", False), ("0.1.18", False),
     ("1.18.0", False), (None, False), ("unknown", False)],
)
def test_only_the_cleared_series_counts_as_cleared(version, cleared):
    assert version_cleared(version) is cleared


def test_detection_runs_graft_version_and_keeps_only_the_version(home):
    host = Host(home, version="0.18.0 (build abc) — see https://example.invalid")

    found = probe_graft(which=host.which, runner=host.run)

    assert found == GraftCli(path=GRAFT, version="0.18.0")
    assert found.cleared is True
    argv, env, _cwd = host.calls[0]
    assert argv == (GRAFT, "--version")
    assert env is not None and env["DO_NOT_TRACK"] == "1"


def test_a_missing_or_broken_graft_is_absent(home):
    missing = Host(home, installed=False)
    assert probe_graft(which=missing.which, runner=missing.run).available is False
    assert missing.calls == []

    broken = Host(home)
    broken_run = lambda argv, **kwargs: CommandOutput(tuple(argv), 127, "", "node: not found")
    assert probe_graft(which=broken.which, runner=broken_run).available is False


# -- accept: the graft CLI -----------------------------------------------------


def test_accepting_installs_the_pinned_series_with_npm_and_turns_telemetry_off(home):
    host = Host(home, installed=False)

    result = graft_cli_step(which=host.which, runner=host.run, home=home).run(None)

    assert result.state is StepState.SUCCEEDED
    assert host.commands(NPM) == [(NPM, "install", "-g", GRAFT_INSTALL_SPEC)]
    assert GRAFT_INSTALL_SPEC == "@nanonets/graft@0.18.x"
    assert (GRAFT, "telemetry", "disable") in host.commands(GRAFT)
    assert json.loads((home / ".graft" / "telemetry.json").read_text())["enabled"] is False
    # Every graft and npm process the installer starts carries DO_NOT_TRACK.
    assert all(env and env.get("DO_NOT_TRACK") == "1" for argv, env, _ in host.calls)
    assert result.detail["installed"] is True and result.detail["telemetry"] == "disabled"
    (package,) = result.resources
    assert (package.kind, package.id, package.owned) == ("npm-package", "@nanonets/graft", True)


def test_an_installed_graft_is_reused_never_upgraded_and_an_uncleared_version_is_drift(home):
    host = Host(home, version="0.20.0")

    result = graft_cli_step(which=host.which, runner=host.run, home=home).run(None)

    assert result.state is StepState.SUCCEEDED
    assert host.commands(NPM) == []
    assert result.detail["cleared"] is False
    assert any("0.20.0 is outside the 0.18.x series" in line for line in result.detail["drift"])
    (package,) = result.resources
    assert (package.owned, package.reused) == (False, True)


def test_telemetry_already_off_is_left_alone(home):
    _telemetry_off(home)
    host = Host(home)

    result = graft_cli_step(which=host.which, runner=host.run, home=home).run(None)

    assert result.state is StepState.SUCCEEDED
    assert host.commands(GRAFT) == [(GRAFT, "--version")]


def test_without_npm_the_step_fails_with_the_way_to_add_it_later(home):
    host = Host(home, installed=False, npm=False)

    result = graft_cli_step(which=host.which, runner=host.run, home=home).run(None)

    assert result.state is StepState.FAILED
    assert "npm is not on PATH" in result.summary
    assert "aq install --with graft" in (result.remediation or "")
    assert host.calls == []


def test_a_failed_npm_install_names_npm_and_the_prefix_fix(home):
    host = Host(home, installed=False, npm_ok=False)

    result = graft_cli_step(which=host.which, runner=host.run, home=home).run(None)

    assert result.state is StepState.FAILED
    assert "EACCES" in result.summary
    assert "npm config set prefix" in (result.remediation or "")


# -- decline -------------------------------------------------------------------


def test_not_selecting_graft_runs_nothing_and_does_not_change_the_outcome(home, tmp_path):
    host = Host(home, installed=False)
    registry = _registry(host, [], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json", capabilities=frozenset())

    assert result.outcome is InstallOutcome.READY
    for step_id in (STEP_GRAFT_CLI, STEP_GRAFT_REPOS):
        assert _step(result, step_id).state is StepState.SKIPPED
        assert "not selected" in _step(result, step_id).summary
    assert host.calls == []


def test_declining_at_the_prompt_runs_nothing(home, tmp_path):
    host = Host(home, installed=False)
    registry = _registry(host, [], vault=tmp_path / "vault")
    asked: list[str] = []

    def decline(step) -> bool:
        asked.append(step.id)
        return False

    result = InstallEngine(
        registry,
        InstallOptions(
            state_path=tmp_path / "state.json",
            capabilities=frozenset({CAPABILITY_GRAFT}),
            interactive=True,
        ),
        support=_support(),
        consent=decline,
    ).run()

    assert result.outcome is InstallOutcome.READY
    assert asked == [STEP_GRAFT_CLI, STEP_GRAFT_REPOS]
    assert _step(result, STEP_GRAFT_CLI).summary == "declined by the operator"
    assert host.commands(NPM) == [] and host.commands(GRAFT) == []


def test_graft_steps_are_advisory_so_a_failure_never_fails_the_install(home, tmp_path):
    host = Host(home, installed=False, npm=False)
    registry = _registry(host, [], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    assert _step(result, STEP_GRAFT_CLI).state is StepState.FAILED
    assert result.outcome is InstallOutcome.READY
    assert result.exit_code == 0
    assert set(result.advisory) >= {STEP_GRAFT_CLI, STEP_GRAFT_REPOS}


# -- main checkouts ------------------------------------------------------------


def test_only_worktree_mode_bases_outside_the_vault_are_main_checkouts(home, tmp_path):
    vault = tmp_path / "vault"
    good = _repo(tmp_path / "dev" / "alpha", home)
    linked = tmp_path / "dev" / "linked"
    linked.mkdir(parents=True)
    (linked / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n")
    plain = tmp_path / "dev" / "plain"
    plain.mkdir(parents=True)
    in_vault = _repo(vault / "projects" / "alpha", home)
    slot = _repo(good / ".aq" / "worktrees" / "slot-0", home)
    rows = [
        _row(good),
        _row(good),  # a duplicate row is one checkout
        _row(slot, role="slot"),
        _row(tmp_path / "snap", mode=None, role="base"),  # a job snapshot
        _row(vault / "projects" / "beta", mode="exclusive-clone"),  # the vault kind
        _row(in_vault, project="vaulted"),
        _row(slot, project="odd"),  # a base row pointing into a slot
        _row(linked, project="linked"),
        _row(plain, project="plain"),
        _row(tmp_path / "gone", project="gone"),
        _row(_repo(tmp_path / "dev" / "off", home), project="off", enabled=False),
    ]

    checkouts, skipped = main_checkouts(rows, vault=vault)

    assert [(c.project_id, c.path) for c in checkouts] == [("alpha", good)]
    reasons = {row["project_id"]: row["reason"] for row in skipped}
    assert reasons == {
        "vaulted": "inside the AQ vault, which graft never indexes",
        "odd": "a worker worktree, where graft stays off",
        "linked": "a linked git worktree, not a main checkout",
        "plain": "not a git repository",
        "gone": "the directory does not exist",
        "off": "the workspace is disabled",
    }


def test_accepting_sets_graft_up_per_repository_only(home, tmp_path):
    """graft init with --no-global in the main checkout; nothing global, no shim."""
    _telemetry_off(home)
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, init_ignores_cache=False)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    step = _step(result, STEP_GRAFT_REPOS)
    assert step.state is StepState.SUCCEEDED, step.summary
    inits = [argv for argv, _env, cwd in host.calls if argv[1:2] == ("init",)]
    assert inits == [(GRAFT, *GRAFT_INIT_ARGS)]
    assert {"--no-global", "--no-statusline"} <= set(inits[0])
    init_cwd = next(cwd for argv, _env, cwd in host.calls if argv[1:2] == ("init",))
    assert init_cwd == str(repo)
    (row,) = step.detail["checkouts"]
    assert row["status"] == "set_up"
    # graft's index was not ignored by the (fake) build, so the installer
    # ignored it locally -- never by editing a tracked file.
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "/graft/" in exclude
    assert not (repo / ".gitignore").exists()
    local = json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert local["env"]["DO_NOT_TRACK"] == "1"
    assert "/.claude/settings.local.json" in exclude
    status = run_command(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=str(repo),
        env=_git_env(home),
    )
    assert ".claude/helpers/graft-hooks.cjs" in status.stdout  # graft's own write
    assert "settings.local.json" not in status.stdout
    assert "graft/" not in status.stdout


def test_a_checkout_graft_already_wired_is_never_re_wired_and_its_drift_is_reported(
    home, tmp_path
):
    _telemetry_off(home)
    repo = _repo(tmp_path / "dev" / "alpha", home)
    # Wired the project-defaults way: committed hooks, a stamp from an old init.
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "node graft-hooks.cjs stop"}]}]}})
    )
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledMcpjsonServers": ["agent-queue"]})
    )
    stamp = repo / "graft" / ".cache" / "wiring-stamp.json"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(json.dumps({"opts": {"global": True, "statusline": True}}))
    (repo / ".gitignore").write_text("/graft/\n.claude/settings.local.json\n")
    host = Host(home)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    step = _step(result, STEP_GRAFT_REPOS)
    assert step.state is StepState.SUCCEEDED
    assert not any(argv[1:2] == ("init",) for argv, _env, _cwd in host.calls)
    (row,) = step.detail["checkouts"]
    assert row["status"] == "already_set_up"
    assert any("global: true" in line for line in step.detail["drift"])
    assert any("statusLine" in line for line in step.detail["drift"])
    # The operator's own local settings survive; only DO_NOT_TRACK is added.
    local = json.loads((repo / ".claude" / "settings.local.json").read_text())
    assert local == {"enabledMcpjsonServers": ["agent-queue"], "env": {"DO_NOT_TRACK": "1"}}
    assert json.loads(stamp.read_text())["opts"]["global"] is True  # reported, not edited


def test_rerunning_reports_the_existing_setup_instead_of_re_wiring(home, tmp_path):
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, installed=False)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")
    state = tmp_path / "state.json"

    first = _install(registry, state)
    assert _step(first, STEP_GRAFT_CLI).state is StepState.SUCCEEDED
    assert _step(first, STEP_GRAFT_REPOS).state is StepState.SUCCEEDED
    host.calls.clear()

    second = _install(registry, state)

    for step_id in (STEP_GRAFT_CLI, STEP_GRAFT_REPOS):
        rerun = _step(second, step_id)
        assert rerun.state is StepState.SUCCEEDED
        assert rerun.detail.get("revalidated") is True
    assert host.commands(NPM) == []
    assert [argv[1:] for argv in host.commands(GRAFT)] == [("--version",)]
    (row,) = _step(second, STEP_GRAFT_REPOS).detail["checkouts"]
    assert row["status"] == "already_set_up"


def test_a_project_added_later_is_set_up_on_the_next_run_and_nothing_else_is_redone(
    home, tmp_path
):
    alpha = _repo(tmp_path / "dev" / "alpha", home)
    beta = _repo(tmp_path / "dev" / "beta", home)
    rows = [_row(alpha)]
    host = Host(home)
    registry = _registry(host, rows, vault=tmp_path / "vault")
    state = tmp_path / "state.json"
    _install(registry, state)
    rows.append(_row(beta, project="beta"))
    host.calls.clear()

    result = _install(registry, state)

    inits = [cwd for argv, _env, cwd in host.calls if argv[1:2] == ("init",)]
    assert inits == [str(beta)]
    statuses = {row["project_id"]: row["status"] for row in _step(result, STEP_GRAFT_REPOS).detail["checkouts"]}
    assert statuses == {"alpha": "already_set_up", "beta": "set_up"}


def test_a_failed_graft_init_is_reported_per_checkout_without_failing_the_install(
    home, tmp_path
):
    _telemetry_off(home)
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, init_ok=False)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    step = _step(result, STEP_GRAFT_REPOS)
    assert step.state is StepState.FAILED
    assert "could not build the graph" in step.summary
    assert str(repo) in (step.remediation or "")
    assert result.outcome is InstallOutcome.READY


def test_a_graft_init_that_exits_zero_but_wires_nothing_is_a_failure(home, tmp_path):
    _telemetry_off(home)
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, init_wires=False)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    step = _step(result, STEP_GRAFT_REPOS)
    assert step.state is StepState.FAILED
    assert "wired nothing" in step.summary
    assert not (repo / ".claude").exists()


def test_without_a_daemon_the_checkout_step_says_how_to_finish(home, tmp_path):
    _telemetry_off(home)
    host = Host(home)
    registry = _registry(
        host, CheckoutListError("no daemon answered at http://127.0.0.1:8081"), vault=tmp_path
    )

    result = _install(registry, tmp_path / "state.json")

    step = _step(result, STEP_GRAFT_REPOS)
    assert step.state is StepState.FAILED
    assert "aq start" in (step.remediation or "")
    assert result.outcome is InstallOutcome.READY


def test_without_graft_the_checkout_step_changes_nothing(home, tmp_path):
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, installed=False)

    result = graft_repos_step(
        which=host.which, runner=host.run, list_workspaces=lambda: [_row(repo)], vault=tmp_path
    ).run(None)

    assert result.state is StepState.SKIPPED
    assert host.calls == []
    assert not (repo / ".claude").exists()


# -- never global --------------------------------------------------------------

GLOBAL_FILES = {
    ".claude/settings.json": json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"command": 'node "~/.claude/helpers/graft-hooks.cjs" stop'}]}]}}
    ),
    ".claude.json": json.dumps({"mcpServers": {"graft": {"command": "graft", "args": ["mcp"]}}}),
    ".codex/config.toml": '[mcp_servers.graft]\ncommand = "graft"\n',
    ".codex/hooks.json": json.dumps({"hooks": {"Stop": [{"command": "graft-hooks stop"}]}}),
    ".config/opencode/opencode.json": json.dumps({"mcp": {"graft": {"type": "local"}}}),
}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".graft" not in path.parts
    }


def test_global_wiring_is_reported_and_never_touched(home, tmp_path):
    for relative, text in GLOBAL_FILES.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    before = _tree(home)
    repo = _repo(tmp_path / "dev" / "alpha", home)
    host = Host(home, installed=False)
    registry = _registry(host, [_row(repo)], vault=tmp_path / "vault")

    result = _install(registry, tmp_path / "state.json")

    # Nothing under $HOME changed, and nothing was added: no shim in
    # ~/.local/bin, no user-level hook helper.  (~/.graft is graft's own
    # telemetry state, written by `graft telemetry disable`.)
    assert _tree(home) == before
    assert not (home / ".local" / "bin" / "graft").exists()
    drift = _step(result, STEP_GRAFT_CLI).detail["drift"]
    assert len(drift) == len(GLOBAL_FILES)
    assert all("remove it by hand" in line for line in drift)


def test_a_clean_home_has_no_global_wiring(home):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(graft:*)"]}})
    )
    assert global_wiring(home) == ()


def test_worker_sessions_carry_the_env_that_keeps_graft_off():
    """The launch half of "graft stays off in worker worktrees" (clear-falcon)."""
    from src.sessions.spec import WORKER_TOOL_ENV

    assert WORKER_TOOL_ENV == {"GRAFT_NO_SEED": "1", "GRAFT_NO_REFRESH": "1", "DO_NOT_TRACK": "1"}


# -- asking the daemon which projects exist ------------------------------------


@pytest.fixture
def daemon():
    """A stand-in for the daemon's /api/execute, recording each command."""
    calls: list[dict] = []
    answers = {
        "list_projects": {"projects": [{"id": "alpha"}, {"id": "beta"}]},
        "list_workspaces": lambda args: {
            "workspaces": [
                {"project_id": args["project_id"], "workspace_path": f"/p/{args['project_id']}",
                 "role": "base", "mode": "worktree"}
            ]
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({**body, "auth": self.headers.get("Authorization")})
            answer = answers.get(body["command"])
            if callable(answer):
                answer = answer(body["args"])
            payload = (
                {"ok": True, "result": answer}
                if answer is not None
                else {"ok": False, "error": f"unknown command {body['command']}"}
            )
            data = json.dumps(payload).encode()
            self.send_response(200 if answer is not None else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls, answers
    finally:
        server.shutdown()
        server.server_close()


def test_projects_are_listed_through_the_command_api_one_project_at_a_time(daemon):
    url, calls, _answers = daemon

    rows = daemon_workspaces(url, environ={"AQ_API_TOKEN": "t0k"})

    assert [row["workspace_path"] for row in rows] == ["/p/alpha", "/p/beta"]
    assert [(call["command"], call["args"]) for call in calls] == [
        ("list_projects", {}),
        ("list_workspaces", {"project_id": "alpha"}),
        ("list_workspaces", {"project_id": "beta"}),
    ]
    assert {call["auth"] for call in calls} == {"Bearer t0k"}


def test_a_command_the_daemon_refuses_is_a_listing_error(daemon):
    url, _calls, answers = daemon
    del answers["list_workspaces"]

    with pytest.raises(CheckoutListError, match="unknown command list_workspaces"):
        daemon_workspaces(url, environ={})


def test_no_daemon_is_a_listing_error():
    with pytest.raises(CheckoutListError, match="no daemon answered"):
        daemon_workspaces("http://127.0.0.1:9", environ={})
