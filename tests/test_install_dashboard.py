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
    failure_excerpt,
    source_checkout_root,
    stamp_path,
)
from src.install.node_toolchain import NODE_ARCHIVES, VERIFIED_MARKER, toolchain_root
from src.install.onboarding import STEP_DAEMON, STEP_DASHBOARD
from src.install.platform import TIER_SUPPORTED, PlatformFacts, SupportVerdict
from src.install.registry import build_registry
from src.install.results import StepState
from src.install.steps import StepContext

BASE = "http://127.0.0.1:8081"
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


def _installed_toolchain(state_dir: Path, system="darwin", arch="arm64") -> Path:
    """A pinned toolchain already unpacked and verified, so no test downloads one."""
    root = toolchain_root(state_dir, system, arch)
    (root / "bin").mkdir(parents=True)
    for name in ("node", "npm"):
        (root / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (root / VERIFIED_MARKER).write_text(NODE_ARCHIVES[(system, arch)][1], encoding="utf-8")
    return root / "bin"


class Host:
    """Records every command, answers git, and builds when the script runs."""

    def __init__(self, root: Path, *, head: str = "abc123"):
        self.root = root
        #: The committed object id of the dashboard's build inputs.
        self.head = head
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


def _which(present=(GIT, AQ)):
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
        probe=host.probe,
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


def test_the_served_check_uses_the_mount_path_with_its_trailing_slash(tmp_path):
    """A bare /dashboard reaches the daemon's catch-all MCP mount on older code."""
    root = _checkout(tmp_path)
    host = Host(root)
    host.daemon_up = True
    seen: list[str] = []
    probe = host.probe
    host.probe = lambda url: (seen.append(url), probe(url))[1]

    _build(tmp_path, host).run(_context())

    dashboard_probes = [url for url in seen if "dashboard" in url]
    assert dashboard_probes and all(url.endswith("/dashboard/") for url in dashboard_probes)


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
