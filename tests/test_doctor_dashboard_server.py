"""``dashboard.server.*`` doctor checks (docs/specs/dashboard-server.md §1, §7 "Doctor").

Each check's pass, warn and fix text, with the probe and the ``aq`` subprocess
replaced -- plus one case for real, where a live dashboard server is killed
the way a crash would kill it and ``--fix`` brings it back.  The checks run
inside the daemon, so the last cases prove they never import
``src.dashboard_server``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.config import DashboardServerConfig
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import apply_fix, run_doctor
from tests.dashboard_server_helpers import unused_port
from tests.test_dashboard_server_app import stage_bundle

# `src.doctor` re-exports the factory under the module's own name.
checks = importlib.import_module("src.doctor.dashboard_server_checks")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pg_backend():
    """Doctor checks here read config and HTTP only; no test database."""


def _ctx(**server: Any) -> DoctorContext:
    config = SimpleNamespace(dashboard_server=DashboardServerConfig(**server))
    return DoctorContext(config=config)  # type: ignore[arg-type]


@pytest.fixture
def bundle(tmp_path, monkeypatch) -> Path:
    staged = stage_bundle(tmp_path)
    monkeypatch.setattr(checks, "_bundle_directory", lambda: staged)
    monkeypatch.setattr(checks, "PID_FILE", tmp_path / "dashboard-server.pid")
    return staged


def _digest(bundle: Path) -> str:
    import hashlib

    return hashlib.sha256((bundle / "aq-dashboard-manifest.json").read_bytes()).hexdigest()


def _ours(pid: int, digest: str | None) -> dict[str, Any]:
    return {
        "service": "aq-dashboard-server",
        "pid": pid,
        "bundle": {"version": "9.9.9", "files": 2, "verified": True, "manifest_sha256": digest},
        "upstream_ok": True,
    }


def _probe(monkeypatch, *answers: tuple[str, dict | None]) -> list[str]:
    """Make the probe answer *answers* in turn (the last one repeats)."""
    seen: list[str] = []
    queue = list(answers)

    def probe(url: str):
        seen.append(url)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(checks, "_probe", probe)
    return seen


def _run(check_id: str, ctx: DoctorContext):
    check = next(c for c in checks.dashboard_server_checks() if c.id == check_id)
    return asyncio.run(check.run(ctx))


def _fix(check_id: str, ctx: DoctorContext):
    check = next(c for c in checks.dashboard_server_checks() if c.id == check_id)
    return asyncio.run(apply_fix(check, ctx))


# ---------------------------------------------------------------------------
# Registration and nothing to check
# ---------------------------------------------------------------------------


def test_the_checks_are_registered_by_the_default_registry():
    ids = set(default_registry().ids())

    assert {checks.RUNNING, checks.BUNDLE, checks.PORT, checks.EXPOSURE, checks.REMOTE_LINK} <= ids


@pytest.mark.parametrize(
    "check_id",
    [checks.RUNNING, checks.BUNDLE, checks.PORT, checks.EXPOSURE, checks.REMOTE_LINK],
)
def test_a_disabled_dashboard_server_is_info_everywhere(check_id, monkeypatch):
    monkeypatch.setattr(checks, "_probe", lambda url: pytest.fail("no probe when disabled"))

    result = _run(check_id, _ctx(enabled=False))

    assert result.severity is Severity.INFO
    assert "dashboard.server.enabled: false" in result.detail


@pytest.mark.parametrize("check_id", [checks.RUNNING, checks.BUNDLE, checks.PORT])
def test_a_source_checkout_without_a_bundle_is_info(check_id, tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "_bundle_directory", lambda: tmp_path / "none")
    monkeypatch.setattr(checks, "_probe", lambda url: pytest.fail("no probe without a bundle"))

    result = _run(check_id, _ctx())

    assert result.severity is Severity.INFO
    assert "no dashboard bundle" in result.detail


# ---------------------------------------------------------------------------
# dashboard.server.running
# ---------------------------------------------------------------------------


def test_a_current_running_server_passes(bundle, monkeypatch):
    seen = _probe(monkeypatch, ("ours", _ours(4242, _digest(bundle))))

    result = _run(checks.RUNNING, _ctx(port=8090))

    assert result.severity is Severity.OK
    assert result.detail == "running at http://127.0.0.1:8090/ (PID 4242), bundle 9.9.9"
    assert seen == ["http://127.0.0.1:8090/"]


def test_a_wildcard_bind_is_probed_on_loopback(bundle, monkeypatch):
    seen = _probe(monkeypatch, ("ours", _ours(1, _digest(bundle))))

    _run(checks.RUNNING, _ctx(host="0.0.0.0", port=8090))

    assert seen == ["http://127.0.0.1:8090/"]


def test_a_stale_build_warns_and_the_fix_restarts_it(bundle, monkeypatch):
    stale = ("ours", _ours(4242, "0" * 64))
    # The check, the fix's own look before acting, then the check after it.
    _probe(monkeypatch, stale, stale, ("ours", _ours(4343, _digest(bundle))))
    ran: list[tuple[str, ...]] = []

    async def run_aq(*args):
        ran.append(args)
        return 0, "Dashboard server started."

    monkeypatch.setattr(checks, "_run_aq", run_aq)

    before = _run(checks.RUNNING, _ctx())
    assert before.severity is Severity.WARN and before.fixable
    assert "older build" in before.detail and "aq dashboard restart" in before.detail

    after = _fix(checks.RUNNING, _ctx())
    assert ran == [("dashboard", "restart")]
    assert after.severity is Severity.OK and after.fix_applied


def test_a_crashed_server_warns_names_the_pid_and_the_fix_starts_it(bundle, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    checks.PID_FILE.write_text(f"{child.pid}\n")
    _probe(monkeypatch, ("none", None), ("none", None), ("ours", _ours(5, _digest(bundle))))
    ran: list[tuple[str, ...]] = []

    async def run_aq(*args):
        ran.append(args)
        return 0, "Dashboard server started."

    monkeypatch.setattr(checks, "_run_aq", run_aq)

    before = _run(checks.RUNNING, _ctx())
    assert before.severity is Severity.WARN and before.fixable
    assert "crashed or was killed" in before.detail and str(child.pid) in before.detail
    assert "aq dashboard start" in before.detail

    after = _fix(checks.RUNNING, _ctx())
    assert ran == [("dashboard", "start")]
    assert after.severity is Severity.OK and after.fix_applied


def test_a_missing_server_warns_and_is_fixable(bundle, monkeypatch):
    _probe(monkeypatch, ("none", None))

    result = _run(checks.RUNNING, _ctx(port=8090))

    assert result.severity is Severity.WARN and result.fixable
    assert result.detail == (
        "the dashboard server is not running at http://127.0.0.1:8090/; "
        "`aq dashboard start` starts it"
    )


def test_a_fix_that_fails_reports_why(bundle, monkeypatch):
    _probe(monkeypatch, ("none", None))

    async def run_aq(*args):
        return 1, "Error: port 8082 answers, but not as the dashboard server"

    monkeypatch.setattr(checks, "_run_aq", run_aq)

    result = _fix(checks.RUNNING, _ctx())

    assert result.severity is Severity.ERROR
    assert "aq dashboard start" in result.detail and "not as the dashboard server" in result.detail


def test_a_port_held_by_another_program_is_not_fixable_here(bundle, monkeypatch):
    _probe(monkeypatch, ("foreign", None))

    running = _run(checks.RUNNING, _ctx(port=8090))
    port = _run(checks.PORT, _ctx(port=8090))
    fixed = _fix(checks.RUNNING, _ctx(port=8090))

    assert running.severity is Severity.WARN and not running.fixable
    assert "dashboard.server.port" in running.detail
    assert fixed.severity is Severity.ERROR and "another program" in fixed.detail
    assert port.severity is Severity.WARN
    assert "port 8090 answers, but not as the dashboard server" in port.detail


def test_a_free_or_owned_port_passes(bundle, monkeypatch):
    _probe(monkeypatch, ("none", None))
    assert _run(checks.PORT, _ctx(port=8090)).detail == "port 8090 is free"
    _probe(monkeypatch, ("ours", _ours(1, None)))
    assert _run(checks.PORT, _ctx(port=8090)).detail == "port 8090 is the dashboard server's"


# ---------------------------------------------------------------------------
# dashboard.server.bundle
# ---------------------------------------------------------------------------


def _rewrite_manifest(bundle: Path, change) -> None:
    path = bundle / "aq-dashboard-manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    change(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_bundle_built_for_slash_passes(bundle):
    result = _run(checks.BUNDLE, _ctx())

    assert result.severity is Severity.OK
    assert result.detail == "bundle 9.9.9 (2 files) built for /"


def test_a_bundle_built_for_the_daemon_mount_warns_with_the_rebuild_command(bundle):
    _rewrite_manifest(bundle, lambda manifest: manifest.pop("base"))

    result = _run(checks.BUNDLE, _ctx())

    assert result.severity is Severity.WARN
    assert "/dashboard mount" in result.detail
    assert "aq install --restart-from dashboard.build" in result.detail


def test_an_unreadable_manifest_warns(bundle):
    (bundle / "aq-dashboard-manifest.json").write_text("{not json", encoding="utf-8")

    result = _run(checks.BUNDLE, _ctx())

    assert result.severity is Severity.WARN
    assert "not valid JSON" in result.detail


# ---------------------------------------------------------------------------
# dashboard.server.exposure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_is_private(host):
    result = _run(checks.EXPOSURE, _ctx(host=host))

    assert result.severity is Severity.OK


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::"])
def test_a_non_loopback_bind_warns_what_it_exposes(host):
    result = _run(checks.EXPOSURE, _ctx(host=host, port=8082))

    assert result.severity is Severity.WARN
    assert "operator console" in result.detail and "no login" in result.detail
    assert "ssh -L 8082:127.0.0.1:8082" in result.detail


# ---------------------------------------------------------------------------
# For real: a dead dashboard server is flagged and fixed
# ---------------------------------------------------------------------------


def test_doctor_flags_and_fixes_a_dead_dashboard_server_for_real(tmp_path, bundle, monkeypatch):
    from src.dashboard_server import process

    state = tmp_path / "state"
    state.mkdir()
    port = unused_port()
    (state / "config.yaml").write_text(
        f"dashboard:\n  server:\n    port: {port}\nmcp_server:\n  port: {unused_port()}\n"
    )
    files = process.ServerFiles.in_state_dir(state)
    monkeypatch.setattr(checks, "PID_FILE", files.pid_file)
    monkeypatch.setattr(process, "bundle_directory", lambda: bundle)

    async def run_aq(*args):
        # What `aq dashboard start` does, against this test's state directory.
        assert args == ("dashboard", "start")
        outcome = await asyncio.to_thread(process.start, files)
        return outcome.exit_code, outcome.message

    monkeypatch.setattr(checks, "_run_aq", run_aq)
    ctx = _ctx(port=port)
    try:
        started = process.start(files)
        assert started.ok, started.message
        assert _run(checks.RUNNING, ctx).severity is Severity.OK

        os.kill(started.pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while process.process_alive(started.pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        report = asyncio.run(run_doctor(_registry(), ctx, only=[checks.RUNNING]))
        (dead,) = report["checks"]
        assert dead["severity"] == "warn" and dead["fixable"]
        assert "crashed or was killed" in dead["detail"] and str(started.pid) in dead["detail"]

        fixed = asyncio.run(run_doctor(_registry(), ctx, only=[checks.RUNNING], fix=True))
        (healed,) = fixed["checks"]
        assert healed["severity"] == "ok" and healed["fix_applied"], healed
        assert fixed["summary"]["fixes_applied"] == 1
        new_pid = process.read_pid_file(files.pid_file)
        assert new_pid not in (None, started.pid) and process.process_alive(new_pid)
    finally:
        process.stop(files)


def _registry():
    from src.doctor.runner import DoctorRegistry

    registry = DoctorRegistry()
    for check in checks.dashboard_server_checks():
        registry.register(check)
    return registry


# ---------------------------------------------------------------------------
# dashboard.remote_link
# ---------------------------------------------------------------------------


def _link_ctx(*, channel: str = "123456789012345678", origins=(), **server: Any) -> DoctorContext:
    from src.config import ApiAuthConfig, HealthCheckConfig

    config = SimpleNamespace(
        dashboard_server=DashboardServerConfig(**server),
        api_auth=ApiAuthConfig(trusted_dashboard_origins=list(origins)),
        discord=SimpleNamespace(channel_id=channel),
        # The daemon's own port: the check must never offer it as a link.
        health_check=HealthCheckConfig(base_url="http://100.99.1.2:8081"),
    )
    return DoctorContext(config=config)  # type: ignore[arg-type]


def test_a_trusted_public_origin_is_ok_and_reachability_stays_unverified(monkeypatch):
    seen = _probe(monkeypatch, ("ours", {"pid": 1}))
    origin = "https://queue.tail1234.ts.net"

    result = _run(checks.REMOTE_LINK, _link_ctx(public_url=origin + "/", origins=[origin]))

    assert result.severity is Severity.OK
    assert origin in result.detail and "unverified" in result.detail
    assert result.data["url"] == origin
    assert result.data["source"] == "dashboard.server.public_url"
    assert result.data["remote_reachability"] == "unverified"
    assert result.data["server"]["health"] == "running"
    assert seen == ["http://127.0.0.1:8082/"]


def test_a_public_origin_the_edge_refuses_warns_with_the_fix(monkeypatch):
    _probe(monkeypatch, ("none", None))
    origin = "https://queue.tail1234.ts.net"

    result = _run(checks.REMOTE_LINK, _link_ctx(public_url=origin))

    assert result.severity is Severity.WARN
    assert "api_auth.trusted_dashboard_origins" in result.detail
    assert result.data["server"]["health"] == "not_running"


def test_no_origin_warns_when_discord_posts_and_never_offers_the_health_port(monkeypatch):
    _probe(monkeypatch, ("ours", {"pid": 1}))

    posting = _run(checks.REMOTE_LINK, _link_ctx())
    silent = _run(checks.REMOTE_LINK, _link_ctx(channel=""))

    assert posting.severity is Severity.WARN
    assert "dashboard.server.public_url" in posting.detail
    assert "docs/guides/dashboard.md#dashboard-links-in-discord-posts" in posting.detail
    assert silent.severity is Severity.INFO and "discord.channel_id" in silent.detail
    for result in (posting, silent):
        assert result.data["url"] is None
        assert "8081" not in json.dumps(result.data) and "8081" not in result.detail


# ---------------------------------------------------------------------------
# The import boundary and the constants shared across it
# ---------------------------------------------------------------------------


def test_the_checks_never_import_the_dashboard_server():
    """They run inside the daemon, whose import path must not load it (spec §1)."""
    code = (
        "import sys\n"
        "import importlib\n"
        "import src.doctor\n"
        "importlib.import_module('src.doctor.dashboard_server_checks')\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('src.dashboard_server')\n"
        "                or m == 'src.dashboard_assets.runtime')\n"
        "print(','.join(loaded))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AQ_", "AGENT_QUEUE_"))}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=60, check=True,
    )

    assert result.stdout.strip() == ""


def test_the_constants_the_checks_copy_agree_with_their_owners():
    import src.cli.daemon as daemon_mod
    from src.dashboard_server import app, bundle, process

    assert checks.SERVICE_NAME == process.SERVICE_NAME == app.SERVICE_NAME
    assert checks.MANIFEST_NAME == process.MANIFEST_NAME == bundle.MANIFEST_NAME
    assert checks.REBUILD_COMMAND == bundle.REBUILD_COMMAND
    assert checks.PID_FILE.name == Path(daemon_mod.DASHBOARD_SERVER_PID_FILE).name
    assert checks._bundle_directory() == process.bundle_directory() == bundle.dashboard_directory()
