"""`dashboard.build` and `dashboard.open`: the dashboard with no hand-run steps.

A source checkout used to finish its install by telling the newcomer to run a
Vite dev server by hand.  These steps build the release's own verified bundle,
restart a daemon that is not serving it, and open it once.  Every host seam --
the command runner, the PATH lookup and the HTTP probe -- is faked, so nothing
here runs npm, starts a daemon or opens a browser.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.install.command import CommandOutput
from src.install.dashboard import (
    STEP_DASHBOARD_BUILD,
    STEP_DASHBOARD_OPEN,
    bundle_directory,
    dashboard_build_step,
    dashboard_open_step,
    source_checkout_root,
    stamp_path,
)
from src.install.onboarding import STEP_DAEMON, STEP_DASHBOARD
from src.install.platform import TIER_SUPPORTED, PlatformFacts, SupportVerdict
from src.install.registry import build_registry
from src.install.results import StepState
from src.install.steps import StepContext

BASE = "http://127.0.0.1:8081"
NPM = "/opt/homebrew/bin/npm"
NODE = "/opt/homebrew/bin/node"
GIT = "/usr/bin/git"
AQ = "/Users/me/.local/bin/aq"


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


def _stage_bundle(root: Path) -> None:
    """What scripts/build_release_artifact.py leaves behind: files plus a manifest."""
    dist = bundle_directory(root)
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>AQ</title>\n", encoding="utf-8")
    files = {"index.html": hashlib.sha256((dist / "index.html").read_bytes()).hexdigest()}
    (dist / "aq-dashboard-manifest.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.1.0", "files": files}), encoding="utf-8"
    )


class Host:
    """Records every command, answers git/node, and builds when the script runs."""

    def __init__(self, root: Path, *, head: str = "abc123", node_version: str = "v22.3.0"):
        self.root = root
        self.head = head
        self.node_version = node_version
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.fail: set[str] = set()
        self.daemon_up = False
        self.serving = False
        self.restart_serves = True

    def run(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        self.calls.append((command, kwargs))

        def result(code=0, stdout=""):
            return CommandOutput(argv=command, returncode=code, stdout=stdout)

        if command[:1] == (GIT,) and "rev-parse" in command:
            return result(stdout=f"{self.head}\n")
        if command[:1] == (GIT,) and "status" in command:
            return result(stdout="")
        if command == (NODE, "--version"):
            return result(stdout=f"{self.node_version}\n")
        for label in self.fail:
            if label in command:
                return result(code=1, stdout=f"{label} exploded")
        if any(part.endswith("build_release_artifact.py") for part in command):
            _stage_bundle(self.root)
        if command == (AQ, "restart", "--no-dashboard"):
            self.serving = self.restart_serves
        return result()

    def commands(self) -> list[tuple[str, ...]]:
        return [command for command, _ in self.calls]

    def probe(self, url: str) -> int | None:
        if not self.daemon_up:
            return None
        if url.endswith("/health"):
            return 200
        return 200 if self.serving else 404


def _which(present=(NPM, NODE, GIT, AQ)):
    table = {Path(path).name: path for path in present}
    return table.get


def _context(*, system="darwin", interactive=True) -> StepContext:
    facts = PlatformFacts(
        system=system,
        release="23.0.0" if system == "darwin" else "6.6.0-microsoft-standard-WSL2",
        machine="arm64" if system == "darwin" else "x86_64",
        arch="arm64" if system == "darwin" else "x86_64",
        python_version="3.12.3",
        wsl=system == "linux",
        wsl_version=2 if system == "linux" else None,
    )
    host_path = "macos-apple-silicon" if system == "darwin" else "windows-wsl2"
    return StepContext(
        support=SupportVerdict(host_path=host_path, tier=TIER_SUPPORTED, facts=facts),
        options={},
        dry_run=False,
        interactive=interactive,
    )


def _build(tmp_path, host, *, which=None, root=None):
    return dashboard_build_step(
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin"},
        home=tmp_path / "aq",
        runner=host.run,
        which=which or _which(),
        probe=host.probe,
        root=root if root is not None else host.root,
        python="/venv/bin/python",
    )


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

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert result.detail["built"] is True
    build_commands = [c for c in host.commands() if c[0] in (NPM, "/venv/bin/python")]
    assert build_commands == [
        (NPM, "ci", "--no-audit", "--no-fund", "--loglevel=error"),
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
            # A Node.js Homebrew installed this run is not on PATH yet.
            assert kwargs["env"]["PATH"].split(":")[0] == str(Path(NPM).parent)
    assert stamp_path(root).read_text(encoding="utf-8").startswith("abc123+")


def test_a_rerun_with_nothing_changed_does_no_work(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())
    host.calls.clear()

    assert step.verify(_context()) is True
    assert not [c for c in host.commands() if c[0] == NPM]


def test_an_updated_checkout_is_rebuilt(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())

    host.head = "def456"  # the bootstrap fast-forwarded the checkout
    assert step.verify(_context()) is False

    host.calls.clear()
    result = step.run(_context())
    assert result.detail["built"] is True
    assert (NPM, "ci", "--no-audit", "--no-fund", "--loglevel=error") in host.commands()
    assert stamp_path(root).read_text(encoding="utf-8").startswith("def456+")


def test_a_running_daemon_that_is_not_serving_the_bundle_is_restarted(tmp_path):
    """The daemon mounts the bundle at start, so one started before it 404s."""
    root = _checkout(tmp_path)
    host = Host(root)
    host.daemon_up = True

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED, result.summary
    assert result.detail["restarted"] is True
    assert (AQ, "restart", "--no-dashboard") in host.commands()


def test_a_current_bundle_the_daemon_is_not_serving_is_not_verified(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host)
    step.run(_context())  # built while no daemon was running

    host.daemon_up = True  # a daemon started later, from before the bundle
    assert step.verify(_context()) is False

    host.calls.clear()
    result = step.run(_context())
    assert result.detail["built"] is False  # nothing rebuilt ...
    assert (AQ, "restart", "--no-dashboard") in host.commands()  # ... only restarted


def test_a_daemon_that_still_does_not_serve_after_a_restart_fails_the_step(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    host.daemon_up = True
    host.restart_serves = False

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "aq restart" in (result.remediation or "")


def test_no_daemon_running_is_not_a_restart(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.SUCCEEDED
    assert not [c for c in host.commands() if c[:1] == (AQ,)]


def test_missing_node_names_the_platform_install_command(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    step = _build(tmp_path, host, which=_which((GIT, AQ)))

    mac = step.run(_context(system="darwin"))
    wsl = step.run(_context(system="linux"))

    assert mac.state is StepState.FAILED and wsl.state is StepState.FAILED
    assert "brew install node" in (mac.remediation or "")
    assert "apt-get install -y nodejs npm" in (wsl.remediation or "")


def test_a_node_too_old_for_the_toolchain_is_refused(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root, node_version="v16.20.2")

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "Node.js 16" in result.summary
    assert not [c for c in host.commands() if c[0] == NPM]


def test_a_failed_stage_names_it_and_leaves_no_stamp(tmp_path):
    root = _checkout(tmp_path)
    host = Host(root)
    host.fail.add("ci")

    result = _build(tmp_path, host).run(_context())

    assert result.state is StepState.FAILED
    assert "install the dashboard's packages" in result.summary
    assert "rerun" in (result.remediation or "")
    assert not stamp_path(root).exists()


def test_this_repository_is_recognised_as_a_source_checkout():
    assert source_checkout_root() == Path(__file__).resolve().parents[1]


# -- dashboard.open ----------------------------------------------------------


def _open(tmp_path, host, *, which=None):
    return dashboard_open_step(
        environ={"HOME": str(tmp_path)},
        home=tmp_path / "aq",
        runner=host.run,
        which=which or (lambda name: f"/usr/bin/{name}"),
        probe=host.probe,
    )


def test_an_interactive_mac_install_opens_the_served_dashboard(tmp_path):
    host = Host(tmp_path)
    host.daemon_up = host.serving = True

    result = _open(tmp_path, host).run(_context(system="darwin"))

    assert result.state is StepState.SUCCEEDED
    assert result.detail["opened"] is True
    assert host.commands() == [("open", f"{BASE}/dashboard/")]


def test_wsl_hands_the_url_to_windows_even_though_explorer_exits_1(tmp_path):
    host = Host(tmp_path)
    host.daemon_up = host.serving = True

    def explorer(argv, **kwargs):
        host.calls.append((tuple(argv), kwargs))
        return CommandOutput(argv=tuple(argv), returncode=1)

    host.run = explorer
    which = {"explorer.exe": "/mnt/c/Windows/explorer.exe"}.get
    result = _open(tmp_path, host, which=which).run(_context(system="linux"))

    assert result.detail["opened"] is True
    assert host.commands() == [("/mnt/c/Windows/explorer.exe", f"{BASE}/dashboard/")]


def test_an_unattended_install_never_opens_a_browser(tmp_path):
    host = Host(tmp_path)
    host.daemon_up = host.serving = True

    result = _open(tmp_path, host).run(_context(interactive=False))

    assert result.state is StepState.SKIPPED
    assert host.calls == []


def test_nothing_is_opened_when_the_dashboard_is_not_served(tmp_path):
    host = Host(tmp_path)
    host.daemon_up = True

    result = _open(tmp_path, host).run(_context())

    assert result.state is StepState.SKIPPED
    assert host.calls == []


def test_a_rerun_never_opens_another_window(tmp_path):
    host = Host(tmp_path)
    assert _open(tmp_path, host).verify(_context()) is True


# -- composition -------------------------------------------------------------


def test_the_dashboard_is_built_after_the_daemon_and_before_it_is_reported_and_opened():
    ids = [step.id for step in build_registry().ordered()]
    order = [STEP_DAEMON, STEP_DASHBOARD_BUILD, STEP_DASHBOARD, STEP_DASHBOARD_OPEN]
    assert [ids.index(step_id) for step_id in order] == sorted(ids.index(s) for s in order)
