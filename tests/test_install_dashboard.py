"""`dashboard.build`, `dashboard.serve` and `dashboard.open`: the dashboard with no hand-run steps.

A source checkout used to finish its install by telling the newcomer to run a
Vite dev server by hand.  These steps build the release's own verified bundle,
start the dashboard server on it (the daemon is API-only and serves no page,
docs/specs/dashboard-server.md), and open it once.  Every host seam -- the
command runner, the PATH lookup, the HTTP probe and the dashboard server's
identity probe -- is faked, so nothing here runs npm, starts a process or opens
a browser.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.install import InstallEngine, InstallOptions, InstallOutcome
from src.install.command import CommandOutput
from src.install.dashboard import (
    STEP_DASHBOARD_BUILD,
    STEP_DASHBOARD_OPEN,
    STEP_DASHBOARD_SERVE,
    bundle_directory,
    dashboard_build_step,
    dashboard_open_step,
    dashboard_serve_step,
    failure_excerpt,
    source_checkout_root,
    stamp_path,
)
from src.install.node_toolchain import NODE_ARCHIVES, VERIFIED_MARKER, toolchain_root
from src.install.onboarding import (
    CAPABILITY_DAEMON,
    DASHBOARD_SERVER,
    STEP_DAEMON,
    STEP_DASHBOARD,
    onboarding_steps,
)
from src.install.platform import TIER_SUPPORTED, PlatformFacts, SupportVerdict
from src.install.prerequisites import data_directory_step, host_step
from src.install.registry import build_registry
from src.install.results import StepState
from src.install.state import load_state, save_state
from src.install.steps import StepContext, StepRegistry

BASE = "http://127.0.0.1:8081"
SERVER = "http://127.0.0.1:8082/"
GIT = "/usr/bin/git"
AQ = "/Users/me/.local/bin/aq"
EXPLORER = "/mnt/c/Windows/explorer.exe"
START = (AQ, "--json", "dashboard", "start")
RESTART = (AQ, "--json", "dashboard", "restart")
VALID_DSN = "postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue"


# -- a fake host -------------------------------------------------------------


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "agent-queue"
    for marker in (
        "package.json",
        "package-lock.json",
        "dashboard/package.json",
        "scripts/build_release_artifact.py",
    ):
        (root / marker).parent.mkdir(parents=True, exist_ok=True)
        (root / marker).write_text("{}\n", encoding="utf-8")
    (root / "src" / "dashboard_assets").mkdir(parents=True)
    return root


def _stage_bundle(root: Path, *, base: str | None = "/", title: str = "AQ") -> None:
    """What scripts/build_release_artifact.py leaves behind: files plus a manifest.

    ``base=None`` is a bundle built for the daemon's old ``/dashboard`` mount,
    whose manifest has no ``base``.
    """
    dist = bundle_directory(root)
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(f"<!doctype html><title>{title}</title>\n", encoding="utf-8")
    files = {"index.html": hashlib.sha256((dist / "index.html").read_bytes()).hexdigest()}
    manifest = {"schema_version": 1, "version": "0.1.0", "files": files}
    if base is not None:
        manifest["base"] = base
    (dist / "aq-dashboard-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _manifest_sha(root: Path) -> str | None:
    try:
        raw = (bundle_directory(root) / "aq-dashboard-manifest.json").read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def _installed_toolchain(state_dir: Path, system="darwin", arch="arm64") -> Path:
    """A pinned toolchain already unpacked and verified, so no test downloads one."""
    root = toolchain_root(state_dir, system, arch)
    (root / "bin").mkdir(parents=True)
    for name in ("node", "npm"):
        (root / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (root / VERIFIED_MARKER).write_text(NODE_ARCHIVES[(system, arch)][1], encoding="utf-8")
    return root / "bin"


class Host:
    """Records every command, answers git, builds when the script runs, and plays
    the daemon and the dashboard server.

    ``served`` is the manifest digest the running dashboard server verified at
    its start (``None`` when it is not running): a rebuild changes the digest
    on disk, not the one the running server serves.
    """

    def __init__(self, root: Path, *, head: str = "abc123", server_url: str = SERVER):
        self.root = root
        #: The committed object id of the dashboard's build inputs.
        self.head = head
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.probes: list[str] = []
        self.fail: set[str] = set()
        self.daemon_up = False
        self.server_url = server_url
        self.served: str | None = None
        #: What `aq dashboard start|restart` does: start one, or fail like the CLI.
        self.start_output: CommandOutput | None = None
        self.start_serves = True
        #: Another program holds the dashboard server's port.
        self.foreign = False
        #: What the running server answers for `GET /`.
        self.page_status = 200

    def run(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        self.calls.append((command, kwargs))

        def result(code=0, stdout=""):
            return CommandOutput(argv=command, returncode=code, stdout=stdout)

        if command[:1] == (GIT,) and "ls-tree" in command:
            return result(stdout=f"040000 tree {self.head}\tdashboard\n")
        if command[:1] == (GIT,) and "status" in command:
            return result(stdout="")
        for label in self.fail:
            if label in command:
                return CommandOutput(
                    argv=command,
                    returncode=2,
                    stdout=(
                        "> tsc -b && vite build\n"
                        "src/App.tsx(3,1): error TS2307: Cannot find module './Missing'.\n"
                    ),
                    stderr="Traceback ...\nsubprocess.CalledProcessError: exit status 2\n",
                )
        if any(part.endswith("build_release_artifact.py") for part in command):
            _stage_bundle(self.root, title=f"AQ {self.head}")
        if command in (START, RESTART):
            if self.start_output is not None:
                return self.start_output
            if self.start_serves:
                self.served = _manifest_sha(self.root)
                self.page_status = 200
            return result(stdout=json.dumps({"schema_version": 1, "data": {"ok": True}}))
        return result()

    def commands(self) -> list[tuple[str, ...]]:
        return [command for command, _ in self.calls]

    def probe(self, url: str) -> int | None:
        self.probes.append(url)
        if url.endswith("/health"):
            return 200 if self.daemon_up else None
        if url == self.server_url:
            if self.served is not None:
                return self.page_status
            return 404 if self.foreign else None
        return 404 if self.daemon_up else None

    def identify(self, url: str):
        self.probes.append(url + "__aq/health")
        if url == self.server_url and self.served is not None:
            return "ours", {
                "service": "aq-dashboard-server",
                "pid": 4242,
                "bundle": {"version": "0.1.0", "verified": True, "manifest_sha256": self.served},
                "upstream_ok": self.daemon_up,
            }
        if self.foreign:
            return "foreign", None
        return "none", None


def _which(present=(GIT, AQ)):
    table = {Path(path).name: path for path in present}
    return table.get


def _facts(system: str) -> PlatformFacts:
    return PlatformFacts(
        system=system,
        release="23.0.0" if system == "darwin" else "6.6.0-microsoft-standard-WSL2",
        machine="arm64" if system == "darwin" else "x86_64",
        arch="arm64" if system == "darwin" else "x86_64",
        python_version="3.12.3",
        wsl=system == "linux",
        wsl_version=2 if system == "linux" else None,
    )


def _support(system: str = "darwin") -> SupportVerdict:
    host_path = "macos-apple-silicon" if system == "darwin" else "windows-wsl2"
    return SupportVerdict(host_path=host_path, tier=TIER_SUPPORTED, facts=_facts(system))


def _context(*, system="darwin", interactive=True) -> StepContext:
    return StepContext(
        support=_support(system),
        options={},
        dry_run=False,
        interactive=interactive,
    )


def _build(tmp_path, host, *, which=None, root=None, fetch=None, toolchain=True):
    state_dir = tmp_path / "aq"
    if toolchain and not toolchain_root(state_dir, "darwin", "arm64").exists():
        _installed_toolchain(state_dir)
        _installed_toolchain(state_dir, "linux", "x86_64")

    def refuse_download(url, destination):
        raise AssertionError(f"no test may download {url}")

    return dashboard_build_step(
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin"},
        home=state_dir,
        runner=host.run,
        which=which or _which(),
        root=root if root is not None else host.root,
        python="/venv/bin/python",
        fetch=fetch or refuse_download,
    )


def _npm(tmp_path, system="darwin", arch="arm64") -> str:
    return str(toolchain_root(tmp_path / "aq", system, arch) / "bin" / "npm")


# -- dashboard.build ---------------------------------------------------------


def test_a_release_install_has_nothing_to_build(tmp_path):
    host = Host(tmp_path)
    step = _build(tmp_path, host, root=tmp_path / "not-a-checkout")

    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED
    assert result.detail["source_checkout"] is False
    assert host.calls == []
    assert step.verify(_context()) is True


def test_a_source_checkout_builds_the_release_bundle_from_inside_the_checkout(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    NPM = _npm(tmp_path)

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert result.detail["built"] is True
    build_commands = [c for c in host.commands() if c[0] in (NPM, "/venv/bin/python")]
    assert build_commands == [
        # AQ's pinned Node, never the machine's; dev dependencies forced in.
        (NPM, "ci", "--include=dev", "--no-audit", "--no-fund", "--loglevel=error"),
        (NPM, "-w", "@aq/ts-client", "run", "generate"),
        (
            "/venv/bin/python",
            str(root / "scripts" / "build_release_artifact.py"),
            "--project-root",
            str(root),
        ),
    ]
    for command, kwargs in host.calls:
        if command[0] in (NPM, "/venv/bin/python"):
            # pip-style relative resolution bit the bootstrap once; npm workspaces
            # resolve from the working directory too.
            assert kwargs["cwd"] == str(root)
            # npm's launcher finds `node` by name: the pinned one comes first.
            assert kwargs["env"]["PATH"].split(":")[0] == str(Path(NPM).parent)
    assert stamp_path(root).read_text(encoding="utf-8").startswith("inputs-")


def test_a_rerun_with_nothing_changed_does_no_work(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())
    host.calls.clear()

    assert step.verify(_context()) is True
    assert not [c for c in host.commands() if c[0] == _npm(tmp_path)]


def test_an_updated_checkout_is_rebuilt(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())
    first = stamp_path(root).read_text(encoding="utf-8")

    host.head = "def456"  # the bootstrap pulled a change to the dashboard
    assert step.verify(_context()) is False

    host.calls.clear()
    result = step.run(_context())
    assert result.detail["built"] is True
    assert any(c[:2] == (_npm(tmp_path), "ci") for c in host.commands())
    assert stamp_path(root).read_text(encoding="utf-8") != first


def test_the_build_touches_neither_the_daemon_nor_the_dashboard_server(tmp_path):
    """The daemon serves no dashboard, so a build is never a reason to restart it."""
    root = _checkout(tmp_path)
    host = Host(root)
    host.daemon_up = True
    host.served = "a-build-from-before"

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert "restarted" not in result.detail
    assert not [c for c in host.commands() if c[:1] == (AQ,)]
    assert host.probes == []


def test_a_bundle_built_for_the_daemon_mount_is_rebuilt(tmp_path):
    """An install from before the dashboard server has a manifest with no `base`.

    Its stamp can even match the checkout (nothing under the build inputs
    moved), and it is still rebuilt: the new verifier refuses it rather than
    serving a page whose assets are addressed under /dashboard/.
    """
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())
    _stage_bundle(root, base=None)  # what the old installer staged
    assert step.verify(_context()) is False

    host.calls.clear()
    result = step.run(_context())

    assert result.detail["built"] is True
    assert json.loads(
        (bundle_directory(root) / "aq-dashboard-manifest.json").read_text(encoding="utf-8")
    )["base"] == "/"
    assert step.verify(_context()) is True


def test_a_failed_toolchain_download_names_the_network_not_the_machine(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)

    def offline(url, destination):
        raise OSError("nodename nor servname provided")

    result = _build(tmp_path, host, fetch=offline, toolchain=False).run(_context())

    assert result.state is StepState.FAILED
    assert "nodejs.org" in result.summary and "nodename" in result.summary
    assert "nodejs.org" in (result.remediation or "")
    assert not [c for c in host.commands() if "ci" in c]


def test_the_linux_host_uses_the_linux_toolchain(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)

    _build(tmp_path, host).run(_context(system="linux"))

    assert any(c[0] == _npm(tmp_path, "linux", "x86_64") for c in host.commands())


def test_a_failed_stage_shows_the_compiler_error_and_keeps_the_full_log(tmp_path):
    """A macOS install once saw only "CalledProcessError ... exit status 2"."""
    root = _checkout(tmp_path)
    host = Host(root)
    host.fail.add(str(root / "scripts" / "build_release_artifact.py"))

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "build and stage the dashboard" in result.summary
    assert "error TS2307: Cannot find module './Missing'" in result.summary
    log = tmp_path / "aq" / "dashboard-build.log"
    assert str(log) in (result.remediation or "")
    assert "error TS2307" in log.read_text(encoding="utf-8")
    assert not stamp_path(root).exists()


def test_the_excerpt_prefers_error_lines_and_drops_stack_frames():
    output = CommandOutput(
        argv=("npm",),
        returncode=1,
        stdout="building...\nerror TS1005: ';' expected.\n    at Object.<anonymous> (x.js:1)\ndone\n",
    )
    assert failure_excerpt(output) == "error TS1005: ';' expected."


def test_this_repository_is_recognised_as_a_source_checkout():
    assert source_checkout_root() == Path(__file__).resolve().parents[1]


# -- dashboard.serve ---------------------------------------------------------


def _serve(tmp_path, host, *, config: str | None = None, which=None, root=None):
    home = tmp_path / "aq"
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    return dashboard_serve_step(
        environ={"HOME": str(tmp_path)},
        home=home,
        runner=host.run,
        which=which or _which(),
        probe=host.probe,
        identify=host.identify,
        root=root if root is not None else host.root,
    )


def _built(tmp_path) -> Host:
    root = _checkout(tmp_path)
    _stage_bundle(root)
    return Host(root)


def test_serve_starts_the_dashboard_server_and_proves_it_serves(tmp_path):
    host = _built(tmp_path)
    step = _serve(tmp_path, host)
    assert step.verify(_context()) is False

    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert host.commands() == [START]
    assert result.detail["url"] == SERVER
    assert result.detail["served"] is True and result.detail["started"] is True
    assert SERVER in result.summary
    # The page and the identity were both checked, and the daemon never was
    # asked for browser content.
    assert SERVER in host.probes and SERVER + "__aq/health" in host.probes
    assert not [url for url in host.probes if url.startswith(BASE)]
    assert step.verify(_context()) is True


def test_a_dashboard_server_already_serving_this_build_is_left_alone(tmp_path):
    host = _built(tmp_path)
    host.served = _manifest_sha(host.root)
    step = _serve(tmp_path, host)

    assert step.verify(_context()) is True
    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED
    assert result.detail["served"] is True
    assert "already serving" in result.summary
    assert host.commands() == []


def test_a_dashboard_server_serving_an_older_build_is_restarted(tmp_path):
    """After a rebuild the running server still serves the manifest it verified."""
    host = _built(tmp_path)
    host.served = "digest-of-the-build-before-the-rebuild"
    step = _serve(tmp_path, host)

    assert step.verify(_context()) is False
    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert host.commands() == [RESTART]
    assert result.summary.startswith("restarted the dashboard server")
    assert step.verify(_context()) is True


def test_a_server_that_answers_its_identity_but_not_the_page_is_restarted(tmp_path):
    host = _built(tmp_path)
    host.served = _manifest_sha(host.root)
    host.page_status = 500
    step = _serve(tmp_path, host)

    assert step.verify(_context()) is False
    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert host.commands() == [RESTART]


def test_the_url_follows_the_dashboard_server_configuration(tmp_path):
    host = _built(tmp_path)
    host.server_url = "http://127.0.0.1:9090/"

    result = _serve(tmp_path, host, config="dashboard:\n  server:\n    port: 9090\n").run(
        _context()
    )

    assert result.state is StepState.SUCCEEDED, result.summary
    assert result.detail["url"] == "http://127.0.0.1:9090/"


def test_a_release_install_serves_the_bundle_it_ships(tmp_path):
    """No checkout, nothing to build -- but the wheel's bundle is served all the same."""
    release = tmp_path / "site-packages"
    _stage_bundle(release)
    host = Host(release)

    result = _serve(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert host.commands() == [START]


def test_no_bundle_reports_the_vite_instructions_without_starting_anything(tmp_path):
    host = Host(_checkout(tmp_path))
    step = _serve(tmp_path, host)

    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED
    assert result.detail["served"] is False
    assert "npm -w dashboard run dev" in result.summary
    assert "aq install --restart-from dashboard.build" in result.summary
    assert host.commands() == []
    assert step.verify(_context()) is True


def test_the_cli_reporting_no_bundle_is_not_a_failure(tmp_path):
    host = _built(tmp_path)
    host.start_output = CommandOutput(argv=START, returncode=2, stdout="{}")

    result = _serve(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED
    assert "npm -w dashboard run dev" in result.summary


def test_a_disabled_dashboard_server_is_left_to_the_operator(tmp_path):
    host = _built(tmp_path)
    step = _serve(tmp_path, host, config="dashboard:\n  server:\n    enabled: false\n")

    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED
    assert result.detail["served"] is False
    assert "dashboard.server.enabled is false" in result.summary
    assert "aq dashboard serve" in result.summary
    assert host.commands() == []
    assert step.verify(_context()) is True


def test_a_misconfigured_dashboard_server_fails_naming_the_key(tmp_path):
    host = _built(tmp_path)
    config = "mcp_server:\n  port: 8081\ndashboard:\n  server:\n    port: 8081\n"

    result = _serve(tmp_path, host, config=config).run(_context())

    assert result.state is StepState.FAILED
    assert "dashboard.server" in result.summary
    assert "config.yaml" in (result.remediation or "")
    assert host.commands() == []


def test_a_port_held_by_another_program_fails_without_starting_anything(tmp_path):
    host = _built(tmp_path)
    host.foreign = True

    result = _serve(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert SERVER in result.summary
    assert "dashboard.server.port" in (result.remediation or "")
    assert host.commands() == []


def test_a_server_that_exits_during_startup_quotes_its_log(tmp_path):
    host = _built(tmp_path)
    envelope = {
        "schema_version": 1,
        "data": None,
        "error": {
            "code": "dashboard_server_exited",
            "message": "The dashboard server exited during startup: bundle digest mismatch",
            "details": {"log_excerpt": ["starting", "index.html: digest mismatch"]},
        },
    }
    host.start_output = CommandOutput(argv=START, returncode=1, stdout=json.dumps(envelope))

    result = _serve(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "exited during startup: bundle digest mismatch" in result.summary
    assert "index.html: digest mismatch" in result.summary
    remediation = result.remediation or ""
    assert "aq dashboard status" in remediation
    assert str(tmp_path / "aq" / "dashboard-server.log") in remediation
    assert "aq restart" not in remediation


def test_a_start_that_succeeds_without_serving_fails_the_step(tmp_path):
    host = _built(tmp_path)
    host.start_serves = False

    result = _serve(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "aq dashboard start" in result.summary
    assert "aq dashboard status" in (result.remediation or "")


def test_serving_is_part_of_starting_aq_in_the_background(tmp_path):
    step = _serve(tmp_path, _built(tmp_path))
    assert step.capability == CAPABILITY_DAEMON
    assert step.mutating and step.consent_prompt


# -- dashboard.open ----------------------------------------------------------


def _open(tmp_path, host, *, which=None):
    return dashboard_open_step(
        environ={"HOME": str(tmp_path)},
        home=tmp_path / "aq",
        runner=host.run,
        which=which or (lambda name: f"/usr/bin/{name}"),
        probe=host.probe,
        identify=host.identify,
    )


def _serving(tmp_path) -> Host:
    host = Host(tmp_path)
    host.daemon_up = True
    host.served = "digest"
    return host


def test_an_interactive_mac_install_opens_the_dashboard_server(tmp_path):
    host = _serving(tmp_path)

    result = _open(tmp_path, host).run(_context(system="darwin"))

    assert result.state is StepState.SUCCEEDED
    assert result.detail["opened"] is True
    assert host.commands() == [("open", SERVER)]


def test_wsl_hands_the_url_to_windows_even_though_explorer_exits_1(tmp_path):
    host = _serving(tmp_path)

    def explorer(argv, **kwargs):
        host.calls.append((tuple(argv), kwargs))
        return CommandOutput(argv=tuple(argv), returncode=1)

    host.run = explorer
    which = {"explorer.exe": "/mnt/c/Windows/explorer.exe"}.get
    result = _open(tmp_path, host, which=which).run(_context(system="linux"))

    assert result.detail["opened"] is True
    assert host.commands() == [("/mnt/c/Windows/explorer.exe", SERVER)]


def test_wsl_prefers_wslview_when_it_is_installed(tmp_path):
    host = _serving(tmp_path)
    which = {"wslview": "/usr/bin/wslview", "explorer.exe": "/mnt/c/Windows/explorer.exe"}.get

    _open(tmp_path, host, which=which).run(_context(system="linux"))

    assert host.commands() == [("/usr/bin/wslview", SERVER)]


def test_an_unattended_install_never_opens_a_browser(tmp_path):
    host = _serving(tmp_path)

    result = _open(tmp_path, host).run(_context(interactive=False))

    assert result.state is StepState.SKIPPED
    assert host.calls == []


def test_nothing_is_opened_when_the_dashboard_server_is_not_serving(tmp_path):
    host = Host(tmp_path)
    host.daemon_up = True  # the daemon answers, but it serves no page

    result = _open(tmp_path, host).run(_context())

    assert result.state is StepState.SKIPPED
    assert host.calls == []


def test_a_rerun_never_opens_another_window(tmp_path):
    host = Host(tmp_path)
    assert _open(tmp_path, host).verify(_context()) is True


# -- composition -------------------------------------------------------------


def test_the_dashboard_is_built_then_served_then_reported_and_opened():
    ids = [step.id for step in build_registry().ordered()]
    order = [
        STEP_DAEMON,
        STEP_DASHBOARD_BUILD,
        STEP_DASHBOARD_SERVE,
        STEP_DASHBOARD,
        STEP_DASHBOARD_OPEN,
    ]
    assert [ids.index(step_id) for step_id in order] == sorted(ids.index(s) for s in order)


# -- an install that relied on the daemon's /dashboard mount, rerun -----------


class Machine(Host):
    """The host plus the daemon's own lifecycle commands, for whole-engine runs."""

    def run(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        if command[1:2] in (("start",), ("restart",)):
            self.calls.append((command, kwargs))
            self.daemon_up = True
            return CommandOutput(argv=command, returncode=0, stdout="Daemon started")
        return super().run(argv, **kwargs)


def _engine_run(tmp_path, machine: Machine, *, interactive: bool):
    home = tmp_path / "aq"
    state_dir = home
    if not toolchain_root(state_dir, "darwin", "arm64").exists():
        _installed_toolchain(state_dir)
        _installed_toolchain(state_dir, "linux", "x86_64")
    registry = StepRegistry((host_step(), data_directory_step(path=home)))
    steps = onboarding_steps(
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin"},
        home=home,
        runner=machine.run,
        which=_which((GIT, AQ, EXPLORER)),
        probe=machine.probe,
        identify=machine.identify,
        dashboard_root=machine.root,
    )
    registry.extend(steps)
    options = InstallOptions(
        installer_version="1.2.3",
        target_version="1.2.3",
        interactive=interactive,
        capabilities=frozenset({CAPABILITY_DAEMON}),
        approve=frozenset({"*"}),
        state_path=home / "install-state.json",
    )
    return InstallEngine(registry, options, support=_support("linux")).run()


def _step(result, step_id):
    return next(row for row in result.steps if row.step_id == step_id)


def test_a_rerun_moves_a_daemon_mount_install_onto_the_dashboard_server(tmp_path):
    """The old installer built a /dashboard/ bundle, opened the daemon's URL and
    recorded no dashboard.serve.  One rerun of the new installer, with no hand-run
    step, rebuilds, starts the dashboard server and reports its URL -- and does
    not open a second browser window.
    """
    home = tmp_path / "aq"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        f"messaging_platform: none\ndatabase:\n  url: {VALID_DSN}\n", encoding="utf-8"
    )
    (home / ".env").write_text("AQ_DB_PASSWORD=local-development\n", encoding="utf-8")
    machine = Machine(_checkout(tmp_path))
    first = _engine_run(tmp_path, machine, interactive=True)
    assert first.outcome is InstallOutcome.READY, first
    assert (EXPLORER, SERVER) in machine.commands()  # the one window an install opens

    # Now make it look like the install the old installer left behind.
    state_path = home / "install-state.json"
    state = load_state(state_path)
    assert state is not None
    del state.steps[STEP_DASHBOARD_SERVE]
    save_state(state, state_path, now="2026-09-16T12:00:00Z")
    _stage_bundle(machine.root, base=None)  # built for the /dashboard/ mount
    machine.served = None  # the old daemon served it; no dashboard server existed
    machine.head = "the-pulled-update"
    machine.calls.clear()

    rerun = _engine_run(tmp_path, machine, interactive=True)

    assert rerun.outcome is InstallOutcome.READY, rerun
    assert _step(rerun, STEP_DASHBOARD_BUILD).detail.get("built") is True
    assert START in machine.commands()
    board = _step(rerun, STEP_DASHBOARD).detail["dashboard"]
    assert board == {"url": SERVER, "reachable": True, "source": DASHBOARD_SERVER, "hint": ""}
    # Interactive, and still no browser: the first install already opened one.
    assert _step(rerun, STEP_DASHBOARD_OPEN).state is StepState.SUCCEEDED
    assert not [c for c in machine.commands() if c[:1] == (EXPLORER,)]
    # And nothing restarted the daemon to make it serve a page.
    assert not [c for c in machine.commands() if c[:2] == (AQ, "restart")]
