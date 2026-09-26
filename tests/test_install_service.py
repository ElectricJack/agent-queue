"""Auto-restart: the watchdog's policy, the service entries it is installed as,
and the rule that a deliberate stop stays stopped.

Nothing here touches the real service manager, crontab or daemon: host
commands go through a scripted runner and every path is under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from src.daemon_state import (
    StopIntent,
    clear_stop_intent,
    pid_is_foreign,
    read_stop_intent,
    record_stop_intent,
)
from src.install import watchdog as wd
from src.install.command import CommandOutput
from src.install.service import (
    CAPABILITY_AUTOSTART,
    CRON_BEGIN,
    CRON_END,
    LAUNCHD_LABEL,
    MECHANISM_CRON,
    MECHANISM_LAUNCHD,
    MECHANISM_SYSTEMD,
    RESOURCE_SERVICE,
    STEP_AUTOSTART,
    UNIT_NAME,
    HostFacts,
    ServicePaths,
    ServiceSpec,
    autostart_step,
    choose_mechanism,
    install_service,
    interpret_status,
    merge_cron_block,
    render_cron_block,
    render_launchd_plist,
    render_systemd_unit,
    service_path,
    strip_cron_block,
    uninstall_service,
)
from src.install.watchdog import (
    CONFIRMATIONS,
    DATABASE_WAIT,
    MAX_START_FAILURES,
    MAX_STARTS_PER_WINDOW,
    Observation,
    Verdict,
    WatchdogState,
    backoff_seconds,
    decide,
    record_start,
)


@pytest.fixture(autouse=True)
def _pg_backend():
    """Nothing here touches a database."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Runner:
    """A scripted command runner: the first matching prefix answers."""

    def __init__(self, answers: dict[tuple[str, ...], CommandOutput] | None = None):
        self.answers = dict(answers or {})
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, argv, *, timeout=30.0, env=None, input_text=None, cwd=None):
        command = tuple(str(part) for part in argv)
        self.calls.append((command, input_text))
        for prefix, output in sorted(self.answers.items(), key=lambda item: -len(item[0])):
            if command[: len(prefix)] == prefix:
                return replace(output, argv=command)
        return CommandOutput(argv=command, returncode=0)

    def ran(self, *prefix: str) -> bool:
        return any(command[: len(prefix)] == prefix for command, _ in self.calls)

    def input_for(self, *prefix: str) -> str | None:
        for command, text in reversed(self.calls):
            if command[: len(prefix)] == prefix:
                return text
        return None


def ok(stdout: str = "") -> CommandOutput:
    return CommandOutput(argv=(), returncode=0, stdout=stdout)


def fail(stderr: str = "", code: int = 1) -> CommandOutput:
    return CommandOutput(argv=(), returncode=code, stderr=stderr)


@pytest.fixture
def paths(tmp_path) -> ServicePaths:
    home = tmp_path / "home"
    home.mkdir()
    return ServicePaths(
        user_home=home, aq_home=home / ".agent-queue", config_home=home / ".config"
    )


@pytest.fixture
def venv(tmp_path) -> Path:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "aq"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def spec(mechanism: str, venv: Path, **overrides) -> ServiceSpec:
    values = {
        "mechanism": mechanism,
        "python": str(venv / "python"),
        "aq": str(venv / "aq"),
        "path": f"{venv}:/usr/bin:/bin",
        "interval": 30.0 if mechanism != MECHANISM_CRON else 120.0,
        "lang": "C.UTF-8",
    }
    values.update(overrides)
    return ServiceSpec(**values)


LINUX_NO_BUS = HostFacts(system="linux", systemd_detail="Failed to connect to bus", crontab=True)
LINUX_SYSTEMD = HostFacts(system="linux", systemd_user=True, systemd_booted=True, crontab=True)
MAC = HostFacts(system="darwin", launchctl=True)


# ---------------------------------------------------------------------------
# The policy: decide()
# ---------------------------------------------------------------------------


def run_checks(observations, *, boot_first=False, interval=30.0, state=None):
    state = state or WatchdogState()
    verdicts = []
    for index, observation in enumerate(observations):
        verdict, _detail, state = decide(
            observation, state, boot=boot_first and index == 0, interval=interval
        )
        verdicts.append(verdict)
    return verdicts, state


def test_a_running_daemon_is_left_alone_and_clears_every_counter():
    stale = WatchdogState(down_checks=1, down_since=1.0, start_failures=3, next_attempt_at=99.0)
    verdict, detail, state = decide(Observation(now=10.0, daemon_pid=4242), stale)
    assert verdict is Verdict.RUNNING and "4242" in detail
    assert (state.down_checks, state.start_failures, state.next_attempt_at) == (0, 0, 0.0)


def test_down_is_confirmed_before_anything_is_started():
    verdicts, _ = run_checks(
        [Observation(now=t, database_ready=True) for t in (0.0, 30.0)]
    )
    assert verdicts == [Verdict.DOWN, Verdict.START]
    assert CONFIRMATIONS == 2


def test_a_boot_check_needs_no_confirmation():
    verdicts, _ = run_checks([Observation(now=0.0, database_ready=True)], boot_first=True)
    assert verdicts == [Verdict.START]


def test_an_explicit_stop_stays_stopped_until_start():
    """The rule the whole feature hangs on: a stop is not a crash."""
    intent = StopIntent(by="aq stop", at=0.0)
    observations = [
        Observation(now=float(t), stop_intent=intent, database_ready=True)
        for t in range(0, 3600 * 24, 30)
    ]
    verdicts, state = run_checks(observations, boot_first=True)
    assert set(verdicts) == {Verdict.STOPPED}
    assert state.down_checks == 0

    # `aq start` removed the marker and the daemon then crashed: now it is a crash.
    after, _ = run_checks(
        [Observation(now=90000.0 + t, database_ready=True) for t in (0.0, 30.0)], state=state
    )
    assert after == [Verdict.DOWN, Verdict.START]


def test_a_stop_during_a_down_series_withdraws_the_confirmation():
    first = Observation(now=0.0, database_ready=True)
    stopped = Observation(now=30.0, stop_intent=StopIntent(by="aq stop", at=29.0))
    verdicts, _ = run_checks([first, stopped, Observation(now=60.0, database_ready=True)])
    assert verdicts == [Verdict.DOWN, Verdict.STOPPED, Verdict.DOWN]


def test_a_start_or_update_in_progress_is_never_raced():
    busy = Observation(now=0.0, busy="an `aq update` is in progress")
    verdicts, _ = run_checks([busy] * 5, boot_first=True)
    assert set(verdicts) == {Verdict.BUSY}


def test_nothing_is_started_before_there_is_a_configuration():
    verdicts, _ = run_checks([Observation(now=0.0, config_present=False)] * 3)
    assert set(verdicts) == {Verdict.UNCONFIGURED}


def test_the_database_is_waited_for_and_then_tried_anyway():
    times = [0.0, 30.0, 60.0, DATABASE_WAIT + 1]
    verdicts, _ = run_checks([Observation(now=t, database_ready=False) for t in times])
    assert verdicts == [
        Verdict.DOWN,
        Verdict.WAITING_FOR_DATABASE,
        Verdict.WAITING_FOR_DATABASE,
        Verdict.START,
    ]


def test_failed_starts_back_off_exponentially_and_then_give_up():
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [60.0, 120.0, 240.0]
    assert backoff_seconds(20) == wd.BACKOFF_MAX

    state = WatchdogState(down_checks=5, down_since=0.0, last_check=0.0)
    now = 0.0
    for attempt in range(1, MAX_START_FAILURES + 1):
        verdict, _, state = decide(Observation(now=now, database_ready=True), state)
        assert verdict is Verdict.START, attempt
        state = record_start(state, now=now, ok=False, message="boom")
        assert state.start_failures == attempt
        verdict, _, state = decide(Observation(now=now + 1, database_ready=True), state)
        assert verdict is (Verdict.BACKOFF if attempt < MAX_START_FAILURES else Verdict.GAVE_UP)
        now = state.next_attempt_at + 1 if attempt < MAX_START_FAILURES else now + 5000
        # stay inside one observation series
        state.last_check = now - 1
    verdict, detail, _ = decide(Observation(now=now, database_ready=True), state)
    assert verdict is Verdict.GAVE_UP and "aq service check --reset" in detail


def test_a_success_clears_the_failures():
    state = record_start(WatchdogState(start_failures=3), now=5.0, ok=True, message="")
    assert (state.start_failures, state.next_attempt_at, state.starts) == (0, 0.0, [5.0])


def test_a_crash_loop_is_rate_limited():
    starts = [float(t) for t in range(MAX_STARTS_PER_WINDOW)]
    state = WatchdogState(starts=starts, down_checks=1, down_since=10.0, last_check=10.0)
    verdict, _, _ = decide(Observation(now=20.0, database_ready=True), state)
    assert verdict is Verdict.CRASH_LOOP
    later = replace(state, last_check=wd.CRASH_WINDOW + 10.0, down_since=wd.CRASH_WINDOW)
    verdict, _, _ = decide(Observation(now=wd.CRASH_WINDOW + 20.0, database_ready=True), later)
    assert verdict is Verdict.START


def test_a_gap_between_checks_breaks_the_confirmation_series():
    state = WatchdogState(down_checks=1, down_since=1.0, last_check=1.0)
    verdict, _, state = decide(Observation(now=5000.0, database_ready=True), state)
    assert verdict is Verdict.DOWN and state.down_since == 5000.0


def test_state_round_trips_and_tolerates_garbage():
    state = WatchdogState(verdict="running", starts=[1.0], last_start={"ok": True})
    assert WatchdogState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
    assert WatchdogState.from_dict("nonsense") == WatchdogState()
    assert WatchdogState.from_dict({"starts": ["x", 2]}).starts == [2.0]


# ---------------------------------------------------------------------------
# One check: tick() with a fake host
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, home: Path, *, pid=None, db=True, start_ok=True):
        self.home = home
        self.pid = pid
        self.db = db
        self.start_ok = start_ok
        self.started = 0
        self.config = True
        self.busy_reason = None

    def daemon_pid(self):
        return self.pid

    def stop_intent(self):
        return read_stop_intent(path=self.home / "daemon.stopped")

    def busy(self):
        return self.busy_reason

    def config_present(self):
        return self.config

    def database_ready(self):
        return self.db

    def start_daemon(self):
        self.started += 1
        if self.start_ok:
            self.pid = 777
            return CommandOutput(argv=("aq", "start"), returncode=0, stdout="Daemon started")
        return CommandOutput(argv=("aq", "start"), returncode=1, stderr="Error: boom")


def _dead_pid() -> int:
    for pid in range(4_194_000, 4_000_000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            continue
    raise AssertionError("no free pid")


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_a_confirmed_crash_is_started_logged_and_recorded(tmp_path):
    host, clock = FakeHost(tmp_path), Clock()
    first = wd.tick(host, now=clock)
    clock.now += 30
    second = wd.tick(host, now=clock)

    assert (first.verdict, second.verdict) == (Verdict.DOWN, Verdict.STARTED)
    assert host.started == 1
    saved = wd.load_state(tmp_path)
    assert saved.verdict == "started" and saved.last_start["ok"] is True
    log = wd.log_path(tmp_path).read_text()
    assert "down: daemon not running (1/2 checks)" in log
    assert "Daemon started" in log and "started: daemon started" in log

    clock.now += 30
    assert wd.tick(host, now=clock).verdict is Verdict.RUNNING
    clock.now += 30
    wd.tick(host, now=clock)
    # A steady state is not re-logged every check.
    assert wd.log_path(tmp_path).read_text().count("running:") == 1


def test_stop_stays_stopped_across_many_checks_and_a_reboot(tmp_path):
    host, clock = FakeHost(tmp_path), Clock()
    record_stop_intent("aq stop", reason="maintenance", path=tmp_path / "daemon.stopped")
    for _ in range(50):
        result = wd.tick(host, now=clock)
        assert result.verdict is Verdict.STOPPED
        clock.now += 30
    assert wd.tick(host, now=clock, boot=True).verdict is Verdict.STOPPED
    assert host.started == 0
    assert "maintenance" in wd.load_state(tmp_path).detail

    clear_stop_intent(path=tmp_path / "daemon.stopped")  # what `aq start` does
    host.pid = 1234
    assert wd.tick(host, now=clock).verdict is Verdict.RUNNING


def test_a_failed_start_is_logged_with_its_output_and_backs_off(tmp_path):
    host, clock = FakeHost(tmp_path, start_ok=False), Clock()
    assert wd.tick(host, now=clock, boot=True).verdict is Verdict.START_FAILED
    clock.now += 30
    assert wd.tick(host, now=clock).verdict is Verdict.BACKOFF
    assert host.started == 1
    log = wd.log_path(tmp_path).read_text()
    assert "Error: boom" in log and "retry in 60s" in log


def test_reset_clears_a_give_up(tmp_path):
    host, clock = FakeHost(tmp_path, start_ok=False), Clock()
    wd.save_state(
        tmp_path,
        WatchdogState(
            start_failures=MAX_START_FAILURES, down_checks=3, down_since=clock.now,
            last_check=clock.now,
        ),
    )
    assert wd.tick(host, now=clock).verdict is Verdict.GAVE_UP
    host.start_ok = True
    result = wd.tick(host, now=clock, reset=True)
    assert result.verdict is Verdict.STARTED and host.started == 1


def test_overlapping_checks_never_both_act(tmp_path):
    host, clock = FakeHost(tmp_path), Clock()
    with wd.exclusive(tmp_path) as owned:
        assert owned
        assert wd.tick(host, now=clock, boot=True).verdict is Verdict.SKIPPED
    assert host.started == 0


def test_the_loop_ends_cleanly_on_a_stop_request_and_reloads_on_new_code(tmp_path):
    host, clock = FakeHost(tmp_path, pid=1), Clock()
    waits = iter([False, True])
    assert wd.run_loop(host, now=clock, wait=lambda seconds: next(waits)) == 0

    changed = iter([False, True])
    code = wd.run_loop(
        host, now=clock, wait=lambda seconds: False, code_changed=lambda: next(changed)
    )
    assert code == wd.EXIT_RELOAD
    assert "restarts it on the new code" in wd.log_path(tmp_path).read_text()


def test_a_check_that_raises_does_not_end_the_loop(tmp_path):
    host, clock = FakeHost(tmp_path), Clock()

    def explode():
        raise RuntimeError("probe broke")

    host.daemon_pid = explode
    waits = iter([False, True])
    assert wd.run_loop(host, now=clock, wait=lambda seconds: next(waits)) == 0
    assert wd.log_path(tmp_path).read_text().count("check failed: RuntimeError") == 2


def test_the_log_is_rotated(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "LOG_MAX_BYTES", 100)
    for index in range(20):
        wd.append_log(tmp_path, f"line {index} " + "x" * 20, now=0.0)
    assert wd.log_path(tmp_path).with_name("aq-service.log.1").exists()
    assert wd.log_path(tmp_path).stat().st_size < 200


# ---------------------------------------------------------------------------
# The real host binding
# ---------------------------------------------------------------------------


def test_the_local_host_reads_the_stop_marker_aq_stop_writes(tmp_path, monkeypatch):
    """`aq stop` and the watchdog agree on where the marker lives."""
    import src.cli.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(tmp_path / "daemon.pid"))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    host = wd.LocalHost(home=tmp_path)

    daemon_mod.stop_daemon(quiet=True)
    assert host.stop_intent() is not None
    assert host.stop_intent().by == "aq stop"

    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(tmp_path / "missing.yaml"))
    daemon_mod.start_daemon()  # fails (no config), but the stop is withdrawn
    assert host.stop_intent() is None


def test_the_local_host_sees_a_start_or_update_in_progress(tmp_path):
    from src.daemon_state import acquire_start_lock

    clock = Clock()
    host = wd.LocalHost(home=tmp_path, clock=clock)
    assert host.busy() is None

    lock = tmp_path / "daemon.lock"
    assert acquire_start_lock(str(lock))  # owned by this (live) process
    os.utime(lock, (clock.now - 86400,) * 2)
    assert "aq start" in host.busy()  # old, but its owner is alive: a long backup

    (lock / "owner").write_text(str(_dead_pid()))
    assert host.busy() is None and not lock.exists()  # owner died: abandoned, removed

    update = tmp_path / "update.lock"
    update.write_text("")
    os.utime(update, (clock.now, clock.now))
    assert "aq update" in host.busy()
    os.utime(update, (clock.now - 2 * wd.UPDATE_LOCK_STALE_AFTER,) * 2)
    assert host.busy() is None and update.exists()  # never removed here


def test_the_local_host_starts_the_daemon_through_aq_start_with_a_clean_environment(tmp_path):
    runner = Runner()
    environ = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path),
        "AQ_SESSION_ID": "worker-1",
        "AQ_SESSION_KIND": "pool",
        "AQ_DB_SCOPE": "worker",
        "CLAUDECODE": "1",
    }
    captured = {}

    def run(argv, **kwargs):
        captured.update(kwargs)
        return runner(argv, **kwargs)

    host = wd.LocalHost(home=tmp_path, aq="/venv/bin/aq", runner=run, environ=environ)
    assert host.start_daemon().ok
    assert runner.calls[0][0] == ("/venv/bin/aq", "start", "--no-dashboard", "--unless-stopped")
    env = captured["env"]
    assert env["PATH"] == "/usr/bin" and env["HOME"] == str(tmp_path)
    assert not {"AQ_SESSION_ID", "AQ_SESSION_KIND", "AQ_DB_SCOPE", "CLAUDECODE"} & set(env)
    assert captured["timeout"] == wd.START_TIMEOUT


def test_the_watchdog_refuses_to_run_inside_a_worker_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AQ_SESSION_KIND", "pool")
    assert wd.main(["check", "--home", str(tmp_path)]) == 10
    assert "worker must never manage" in capsys.readouterr().err
    assert not (tmp_path / "service").exists()


def test_a_reused_pid_is_not_mistaken_for_the_daemon(tmp_path):
    """After a reboot daemon.pid names whatever process got that number."""
    config = str(tmp_path / "config.yaml")
    if not Path(f"/proc/{os.getpid()}/cmdline").exists():
        pytest.skip("needs /proc")
    assert pid_is_foreign(os.getpid(), config)  # this pytest process
    from src.daemon_state import read_daemon_pid

    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))
    assert read_daemon_pid(str(pid_file), config) is None
    assert not pid_file.exists()


def test_a_torn_stop_marker_still_counts_as_a_stop(tmp_path):
    marker = tmp_path / "daemon.stopped"
    marker.write_text("{not json")
    assert read_stop_intent(path=marker) is not None


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


def test_the_systemd_unit_restarts_the_watchdog_and_never_kills_the_daemon(paths, venv):
    unit = render_systemd_unit(spec(MECHANISM_SYSTEMD, venv), paths)
    assert "Restart=on-failure" in unit
    assert "KillMode=process" in unit
    assert "WantedBy=default.target" in unit
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_line.removeprefix("ExecStart=")) == [
        str(venv / "python"), "-P", "-m", "src.install.watchdog", "run",
        "--interval", "30", "--aq", str(venv / "aq"),
    ]
    assert f'Environment="PATH={venv}:/usr/bin:/bin"' in unit
    assert f"StandardOutput=append:{paths.log_path}" in unit


def test_systemd_specifiers_and_quotes_are_escaped(paths, venv):
    unit = render_systemd_unit(spec(MECHANISM_SYSTEMD, venv, path='/opt/50%"odd'), paths)
    assert 'Environment="PATH=/opt/50%%\\"odd"' in unit


def test_the_launchd_agent_keeps_the_watchdog_alive_and_abandons_the_daemon(paths, venv):
    payload = plistlib.loads(render_launchd_plist(spec(MECHANISM_LAUNCHD, venv), paths))
    assert payload["Label"] == LAUNCHD_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["AbandonProcessGroup"] is True
    assert payload["ProgramArguments"][:5] == [
        str(venv / "python"), "-P", "-m", "src.install.watchdog", "run",
    ]
    assert payload["StandardErrorPath"] == str(paths.log_path)
    assert payload["EnvironmentVariables"]["PATH"] == f"{venv}:/usr/bin:/bin"


def test_the_cron_block_checks_at_boot_and_every_few_minutes(paths, venv):
    block = render_cron_block(spec(MECHANISM_CRON, venv), paths)
    lines = block.splitlines()
    assert lines[0] == CRON_BEGIN and lines[-1] == CRON_END
    boot, every = lines[1], lines[2]
    assert boot.startswith("@reboot ") and "--boot" in boot
    assert every.startswith("*/2 * * * * ") and "--boot" not in every
    for line in (boot, every):
        assert "-P -m src.install.watchdog check" in line
        assert f"2>> {paths.log_path}" in line and "> /dev/null" in line
        # The PATH is read from the install record, not written on the line:
        # a WSL PATH is longer than cron accepts for a command.
        assert "PATH=" not in line


def test_a_percent_sign_cannot_end_a_cron_command(paths, venv):
    block = render_cron_block(spec(MECHANISM_CRON, venv, aq="/opt/100%/aq"), paths)
    assert "/opt/100\\%/aq" in block


def test_every_minute_is_written_as_a_plain_schedule(paths, venv):
    block = render_cron_block(spec(MECHANISM_CRON, venv, interval=30.0), paths)
    assert "\n* * * * * " in block


def test_merging_the_cron_block_keeps_the_operators_entries_and_is_idempotent(paths, venv):
    mine = "MAILTO=me\n# quilt watchdog\n*/2 * * * * $HOME/.quilt/bin/coord-watchdog.sh"
    block = render_cron_block(spec(MECHANISM_CRON, venv), paths)
    once, _ = merge_cron_block(mine, block)
    twice, _ = merge_cron_block(
        once, render_cron_block(spec(MECHANISM_CRON, venv, interval=180), paths)
    )
    assert once.startswith(mine + "\n")
    assert twice.count(CRON_BEGIN) == 1 and "*/3 * * * *" in twice
    stripped, removed = strip_cron_block(twice)
    assert removed and stripped == mine + "\n"


def test_the_service_path_drops_session_entries_and_leads_with_aq(tmp_path, venv):
    shim = tmp_path / ".claude" / "plugins" / "cache" / "x" / "bin"
    shim.mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    environ = {"PATH": os.pathsep.join([str(tools), str(shim), "/does/not/exist", str(tools)])}
    entries = service_path(environ, aq=str(venv / "aq")).split(os.pathsep)
    assert entries[0] == str(venv)
    assert str(tools) in entries and entries.count(str(tools)) == 1
    assert str(shim) not in entries and "/does/not/exist" not in entries
    assert "/usr/bin" in entries


# ---------------------------------------------------------------------------
# Choosing a mechanism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (MAC, MECHANISM_LAUNCHD),
        (LINUX_SYSTEMD, MECHANISM_SYSTEMD),
        (LINUX_NO_BUS, MECHANISM_CRON),
        (HostFacts(system="linux"), None),
    ],
    ids=["macos", "systemd-user-bus", "wsl-without-user-bus", "nothing"],
)
def test_the_best_available_mechanism_is_chosen(facts, expected):
    mechanism, reason = choose_mechanism(facts)
    assert mechanism == expected
    assert reason


def test_a_requested_mechanism_that_is_missing_is_refused_with_the_reason():
    mechanism, reason = choose_mechanism(LINUX_NO_BUS, MECHANISM_SYSTEMD)
    assert mechanism is None and "Failed to connect to bus" in reason
    assert choose_mechanism(LINUX_SYSTEMD, MECHANISM_CRON)[0] == MECHANISM_CRON
    assert choose_mechanism(LINUX_SYSTEMD, MECHANISM_LAUNCHD)[0] is None


# ---------------------------------------------------------------------------
# Install and uninstall
# ---------------------------------------------------------------------------


def _install(paths, venv, runner, facts, **kwargs):
    return install_service(
        aq=str(venv / "aq"),
        python=str(venv / "python"),
        environ={"PATH": f"{venv}:/usr/bin:/bin", "LANG": "C.UTF-8"},
        runner=runner,
        paths=paths,
        facts=facts,
        user="operator",
        **kwargs,
    )


def test_cron_install_appends_a_block_and_leaves_the_rest_of_the_crontab(paths, venv):
    existing = "# mine\n@reboot /usr/local/bin/other.sh\n"
    runner = Runner({("crontab", "-l"): ok(existing)})

    action = _install(paths, venv, runner, LINUX_NO_BUS)

    assert action.ok and action.mechanism == MECHANISM_CRON
    written = runner.input_for("crontab", "-")
    assert written.startswith(existing) and written.count(CRON_BEGIN) == 1
    record = json.loads(paths.record_path.read_text())
    assert record["mechanism"] == MECHANISM_CRON and record["interval"] == 120.0
    assert paths.log_path.parent.is_dir()


def test_cron_install_starts_from_an_empty_crontab(paths, venv):
    runner = Runner({("crontab", "-l"): fail("no crontab for operator")})
    action = _install(paths, venv, runner, LINUX_NO_BUS)
    assert action.ok
    assert runner.input_for("crontab", "-").startswith(CRON_BEGIN)


def test_an_unreadable_crontab_is_never_overwritten(paths, venv):
    runner = Runner({("crontab", "-l"): fail("crontab: permission denied")})
    action = _install(paths, venv, runner, LINUX_NO_BUS)
    assert not action.ok and "permission denied" in action.summary
    assert not runner.ran("crontab", "-") or runner.input_for("crontab", "-") is None
    assert not paths.record_path.exists()


def test_cron_install_warns_when_cron_is_not_running(paths, venv):
    runner = Runner({("crontab", "-l"): ok(""), ("pgrep",): fail()})
    action = _install(paths, venv, runner, LINUX_NO_BUS)
    assert action.ok
    assert any("cron is not running" in note for note in action.notes)


def test_systemd_install_writes_and_enables_the_unit_and_asks_for_linger(paths, venv, tmp_path):
    linger = tmp_path / "linger"
    linger.mkdir()
    runner = Runner(
        {
            ("crontab", "-l"): ok("# unrelated\n" + render_cron_block(spec(MECHANISM_CRON, venv), paths)),
            ("loginctl",): fail("Access denied"),
        }
    )

    action = _install(paths, venv, runner, LINUX_SYSTEMD, linger_root=linger)

    assert action.ok and action.mechanism == MECHANISM_SYSTEMD
    assert "KillMode=process" in paths.unit_path.read_text()
    for command in (
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", UNIT_NAME),
        ("systemctl", "--user", "restart", UNIT_NAME),
        ("loginctl", "enable-linger", "operator"),
        ("sudo", "-n", "loginctl", "enable-linger", "operator"),
    ):
        assert runner.ran(*command), command
    # The old cron watchdog was removed: one watchdog per host.
    assert CRON_BEGIN not in runner.input_for("crontab", "-")
    assert runner.input_for("crontab", "-").startswith("# unrelated")


def test_systemd_install_skips_linger_when_it_is_already_on(paths, venv, tmp_path):
    linger = tmp_path / "linger"
    linger.mkdir()
    (linger / "operator").write_text("")
    runner = Runner({("crontab", "-l"): ok("")})
    assert _install(paths, venv, runner, LINUX_SYSTEMD, linger_root=linger).ok
    assert not runner.ran("loginctl")


def test_a_failing_systemctl_fails_the_install_with_its_message(paths, venv):
    runner = Runner({("systemctl", "--user", "enable"): fail("Unit file is masked.")})
    action = _install(paths, venv, runner, LINUX_SYSTEMD)
    assert not action.ok and "masked" in action.summary
    assert not paths.record_path.exists()


def test_launchd_install_bootstraps_the_agent(paths, venv):
    runner = Runner({("launchctl", "bootout"): fail("No such process")})
    action = _install(paths, venv, runner, MAC)
    assert action.ok and action.mechanism == MECHANISM_LAUNCHD
    assert paths.plist_path.exists()
    assert runner.ran("launchctl", "bootstrap")


def test_no_mechanism_needs_the_user(paths, venv):
    action = _install(paths, venv, Runner(), HostFacts(system="linux"))
    assert not action.ok and action.needs_user
    assert "cron" in action.remediation


def test_a_dry_run_changes_nothing_and_shows_the_entry(paths, venv):
    runner = Runner({("crontab", "-l"): ok("# mine\n")})
    action = _install(paths, venv, runner, LINUX_NO_BUS, dry_run=True)
    assert action.ok and action.dry_run
    assert CRON_BEGIN in action.preview
    assert runner.input_for("crontab", "-") is None
    assert not paths.record_path.exists()


def test_uninstall_removes_every_entry_and_leaves_the_daemon_alone(paths, venv):
    installed = "# mine\n" + render_cron_block(spec(MECHANISM_CRON, venv), paths)
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    paths.record_path.parent.mkdir(parents=True)
    paths.record_path.write_text("{}")
    runner = Runner({("crontab", "-l"): ok(installed)})

    action = uninstall_service(runner=runner, paths=paths)

    assert action.ok
    assert not paths.unit_path.exists() and not paths.record_path.exists()
    assert runner.ran("systemctl", "--user", "disable", "--now", UNIT_NAME)
    assert runner.input_for("crontab", "-") == "# mine\n"
    assert not runner.ran("aq")  # never `aq stop`


def test_uninstall_with_nothing_installed_is_a_no_op(paths):
    runner = Runner({("crontab", "-l"): fail("no crontab for operator")})
    action = uninstall_service(runner=runner, paths=paths)
    assert action.ok and "no AQ watchdog" in action.summary
    assert not runner.ran("crontab", "-")


# ---------------------------------------------------------------------------
# Status (what `aq service status` and the doctor check report)
# ---------------------------------------------------------------------------


def _record(paths: ServicePaths, mechanism: str, **extra) -> dict:
    return {
        "mechanism": mechanism,
        "python": "/bin/sh",
        "aq": "/bin/sh",
        "interval": 30.0,
        "installed_at": 0.0,
        **extra,
    }


NOW = 10_000.0


def test_status_when_nothing_is_installed(paths):
    status = interpret_status(
        paths, record=None, outputs={"crontab": ok("")}, watchdog=WatchdogState(), now=NOW
    )
    assert not status.installed and "aq service install" in status.summary


def test_a_running_systemd_watchdog_is_healthy(paths):
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    status = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled\n"), "systemd.active": ok("active\n")},
        watchdog=WatchdogState(last_check=NOW - 20, verdict="running", interval=30.0),
        now=NOW,
        linger=True,
    )
    assert status.healthy, status.problems
    assert "systemd user unit" in status.summary


def test_a_systemd_watchdog_without_linger_needs_attention(paths):
    """Without linger, logging out kills the daemon the watchdog started."""
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    status = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled"), "systemd.active": ok("active")},
        watchdog=WatchdogState(last_check=NOW - 20, verdict="running", interval=30.0),
        now=NOW,
        linger=False,
    )
    assert not status.healthy and "linger is off" in status.problems[0]


def test_an_inactive_unit_or_a_silent_watchdog_needs_attention(paths):
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    status = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled"), "systemd.active": fail(code=3) },
        watchdog=WatchdogState(last_check=NOW - 20, verdict="running"),
        now=NOW,
    )
    assert not status.healthy and "not running" in status.problems[0]

    silent = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled"), "systemd.active": ok("active")},
        watchdog=WatchdogState(last_check=NOW - 3600, verdict="running", interval=30.0),
        now=NOW,
    )
    assert not silent.healthy and "last checked" in silent.problems[0]


def test_cron_entries_without_a_running_cron_need_attention(paths, venv):
    block = render_cron_block(spec(MECHANISM_CRON, venv), paths)
    status = interpret_status(
        paths,
        record=_record(paths, MECHANISM_CRON, interval=120.0),
        outputs={"crontab": ok(block), "cron": fail(), "crond": fail()},
        watchdog=WatchdogState(last_check=NOW - 60, verdict="running", interval=120.0),
        now=NOW,
    )
    assert not status.healthy and "cron is not running" in status.problems[0]


def test_a_removed_entry_or_a_give_up_needs_attention(paths):
    gone = interpret_status(
        paths,
        record=_record(paths, MECHANISM_CRON),
        outputs={"crontab": ok("# nothing of ours\n"), "cron": ok()},
        watchdog=WatchdogState(last_check=NOW - 10, verdict="running"),
        now=NOW,
    )
    assert not gone.healthy and "entry is gone" in gone.problems[0]

    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    gave_up = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled"), "systemd.active": ok("active")},
        watchdog=WatchdogState(last_check=NOW - 10, verdict="gave_up", detail="5 starts failed"),
        now=NOW,
    )
    assert not gave_up.healthy and "gave up" in gave_up.problems[0]


def test_status_reports_a_deliberate_stop(paths):
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    status = interpret_status(
        paths,
        record=_record(paths, MECHANISM_SYSTEMD),
        outputs={"systemd.enabled": ok("enabled"), "systemd.active": ok("active")},
        watchdog=WatchdogState(last_check=NOW - 10, verdict="stopped"),
        now=NOW,
        stop_intent=StopIntent(by="aq stop", at=NOW - 100),
    )
    assert status.healthy
    assert any("stopped on purpose by aq stop" in note for note in status.notes)


async def test_the_doctor_check_reports_not_installed_as_info_and_breakage_as_warn(
    paths, monkeypatch
):
    import importlib

    from src.doctor.models import DoctorContext, Severity
    from src.install import service as service_mod

    # `src.doctor.service_checks` the attribute is the function of that name.
    service_checks = importlib.import_module("src.doctor.service_checks")
    monkeypatch.setattr(service_mod.ServicePaths, "for_host", classmethod(lambda cls: paths))

    async def run(argv):
        if argv[:2] == ["crontab", "-l"]:
            return ok("")
        if argv[:1] == ["pgrep"]:
            return ok("1")
        return fail("Failed to connect to bus")

    monkeypatch.setattr(service_checks, "_run", run)
    monkeypatch.setattr(service_checks.sys, "platform", "linux")
    result = await service_checks._check_autostart(DoctorContext(config=None))
    assert result.id == "daemon.autostart"
    assert result.severity is Severity.INFO and "aq service install" in result.detail

    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    paths.record_path.parent.mkdir(parents=True)
    paths.record_path.write_text(json.dumps(_record(paths, MECHANISM_SYSTEMD)))
    result = await service_checks._check_autostart(DoctorContext(config=None))
    assert result.severity is Severity.WARN
    assert "not enabled" in result.detail or "not running" in result.detail
    assert result.data["mechanism"] == MECHANISM_SYSTEMD


def test_the_doctor_check_is_registered():
    from src.doctor import default_registry

    assert default_registry().get("daemon.autostart") is not None


# ---------------------------------------------------------------------------
# aq install and aq uninstall
# ---------------------------------------------------------------------------


def test_the_install_step_is_opt_in_after_the_daemon_and_never_halts():
    from src.install import build_registry, describe_host

    step = build_registry(describe_host()).get(STEP_AUTOSTART)
    assert step.capability == CAPABILITY_AUTOSTART
    assert step.depends_on == ("daemon.start",)
    assert step.mutating and not step.halts


def test_the_install_step_installs_and_records_the_service(paths, venv, monkeypatch):
    from src.install.platform import describe_host
    from src.install.steps import StepContext

    runner = Runner({("crontab", "-l"): ok("")})
    step = autostart_step(
        which=lambda name: str(venv / name) if name == "aq" else None,
        runner=runner,
        paths=paths,
        facts=LINUX_NO_BUS,
        environ={"PATH": f"{venv}:/usr/bin:/bin"},
    )
    context = StepContext(
        support=describe_host(), options={"autostart": {"interval": 180}},
        dry_run=False, interactive=False,
    )
    result = step.run(context)
    assert result.state.value == "succeeded", result.summary
    assert result.resources[0].kind == RESOURCE_SERVICE
    assert result.resources[0].id == MECHANISM_CRON
    assert "*/3 * * * *" in runner.input_for("crontab", "-")

    bad = StepContext(
        support=describe_host(), options={"autostart": {"mechansim": "cron"}},
        dry_run=False, interactive=False,
    )
    assert step.run(bad).state.value == "failed"


def test_uninstall_removes_the_service_first(tmp_path):
    from src.install.lifecycle import RemovalAction, default_handlers, plan_uninstall
    from src.install.results import ResourceRecord
    from src.install.state import InstallState

    state = InstallState(installer_version="test", target_version="test")
    for record in (
        ResourceRecord(kind="daemon", id="http://127.0.0.1:8081"),
        ResourceRecord(kind=RESOURCE_SERVICE, id=MECHANISM_CRON),
    ):
        state.resources[record.key] = record
    plan = plan_uninstall(state)
    kinds = [item.kind for item in plan.removals]
    assert kinds[:2] == [RESOURCE_SERVICE, "daemon"]
    assert all(item.action is RemovalAction.REMOVE for item in plan.removals)
    assert RESOURCE_SERVICE in default_handlers(home=tmp_path)


def test_the_wizard_offers_autostart_only_under_advanced_and_defaults_it_off():
    from src.install.wizard import question_plan, questions_to_ask

    questions = question_plan()
    autostart = next(q for q in questions if q.id == "autostart")
    assert autostart.capability == CAPABILITY_AUTOSTART
    assert autostart.default is False and autostart.advanced
    assert autostart not in questions_to_ask(questions, advanced=False)


# ---------------------------------------------------------------------------
# aq service
# ---------------------------------------------------------------------------


@pytest.fixture
def operator_shell(monkeypatch):
    from src.sessions.env import DAEMON_ENV_STRIP_KEYS

    for key in DAEMON_ENV_STRIP_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_aq_service_status_answers_without_a_daemon(paths, monkeypatch):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.install import service as service_mod

    monkeypatch.setattr(service_mod.ServicePaths, "for_host", classmethod(lambda cls, *a: paths))
    monkeypatch.setattr(service_mod, "run_command", Runner({("crontab", "-l"): ok("")}))

    result = CliRunner().invoke(cli, ["--json", "service", "status"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["installed"] is False and "aq service install" in data["summary"]


def test_a_worker_cannot_install_the_service(monkeypatch):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.install import service as service_mod

    monkeypatch.setenv("AQ_SESSION_KIND", "pool")
    called = []
    monkeypatch.setattr(service_mod, "install_service", lambda **kw: called.append(kw))

    result = CliRunner().invoke(cli, ["service", "install"])

    assert result.exit_code == 10
    assert called == []


def test_aq_service_install_reports_the_action_and_its_exit_code(monkeypatch, operator_shell):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.install import service as service_mod
    from src.install.service import ServiceAction

    seen = {}

    def fake_install(**kwargs):
        seen.update(kwargs)
        return ServiceAction(
            ok=False, needs_user=True, summary="no service mechanism is available: x",
            remediation="Install cron [boot] systemd=true",
        )

    monkeypatch.setattr(service_mod, "install_service", fake_install)
    result = CliRunner().invoke(cli, ["service", "install", "--mechanism", "cron", "--interval", "60"])

    assert result.exit_code == 10, result.output
    assert seen["requested"] == "cron" and seen["interval"] == 60.0
    assert "[boot] systemd=true" in result.output  # not eaten as Rich markup


# ---------------------------------------------------------------------------
# End to end: the real host binding against real processes
# ---------------------------------------------------------------------------


def test_the_watchdog_brings_a_crashed_daemon_back_and_leaves_a_stopped_one(
    tmp_path, operator_shell
):
    """A stand-in `aq start` launches a process that looks like the daemon; the
    database is a real listening socket.  Crash -> started; stop -> left down."""
    if not Path("/proc/self/cmdline").exists():
        pytest.skip("identifies the daemon through /proc")
    import signal
    import socket
    import time

    home = tmp_path / "aq-home"
    home.mkdir()
    database = socket.socket()
    database.bind(("127.0.0.1", 0))
    database.listen()
    port = database.getsockname()[1]
    (home / "config.yaml").write_text(f"database:\n  url: postgresql://aq@127.0.0.1:{port}/aq\n")
    fake_aq = tmp_path / "aq"
    # Like `aq start`, it records the PID only once the process is the daemon
    # (Popen returns after exec) and returns only once it is up.
    fake_aq.write_text(
        "#!/bin/bash\n"
        '[ "$1" = start ] || exit 2\n'
        "( exec -a agent-queue sleep 120 ) </dev/null >/dev/null 2>&1 &\n"
        "pid=$!\n"
        "for _ in $(seq 100); do\n"
        "  tr '\\0' ' ' < /proc/$pid/cmdline | grep -q '^agent-queue ' && break\n"
        "  sleep 0.02\n"
        "done\n"
        f'echo $pid > "{home}/daemon.pid"\n'
        'echo "Daemon started (PID $pid)"\n'
    )
    fake_aq.chmod(0o755)
    host = wd.LocalHost(home=home, aq=str(fake_aq))

    seen: list[int] = []

    def daemon_pid():
        pid = int((home / "daemon.pid").read_text())
        seen.append(pid)
        return pid

    try:
        started = wd.tick(host, now=time.time, boot=True)
        assert started.verdict is Verdict.STARTED, started.detail
        first = daemon_pid()
        assert host.daemon_pid() == first

        os.kill(first, signal.SIGKILL)  # a crash
        for _ in range(50):
            if host.daemon_pid() is None:
                break
            time.sleep(0.05)
        assert wd.tick(host, now=time.time).verdict is Verdict.DOWN
        assert wd.tick(host, now=time.time).verdict is Verdict.STARTED
        second = daemon_pid()
        assert second != first

        record_stop_intent("aq stop", path=home / "daemon.stopped")  # what `aq stop` does
        os.kill(second, signal.SIGTERM)
        for _ in range(50):
            if host.daemon_pid() is None:
                break
            time.sleep(0.05)
        for _ in range(3):
            assert wd.tick(host, now=time.time, boot=True).verdict is Verdict.STOPPED
        assert host.daemon_pid() is None  # nothing new was started
        log = wd.log_path(home).read_text()
        assert log.count("started: daemon started") == 2
        assert "stopped on purpose by aq stop" in log
    finally:
        database.close()
        for pid in seen:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Review findings: races, outages, long PATHs, linger, uninstall, damaged cron
# ---------------------------------------------------------------------------


def test_a_database_outage_is_waited_out_and_never_gives_up():
    """Starts that fail while the database is down back off but never count."""
    state = WatchdogState(down_checks=5, down_since=0.0, last_check=0.0)
    now = DATABASE_WAIT + 1
    verdicts = []
    for _ in range(40):
        state.last_check = now - 1
        verdict, _, state = decide(Observation(now=now, database_ready=False), state)
        verdicts.append(verdict)
        if verdict is Verdict.START:
            state = record_start(state, now=now, ok=False, message="db down", database_ready=False)
            assert state.next_attempt_at - now <= wd.BACKOFF_MAX
        now = max(now + 60, state.next_attempt_at + 1)
    assert Verdict.GAVE_UP not in verdicts
    assert verdicts.count(Verdict.START) > MAX_START_FAILURES
    assert state.start_failures == 0


def test_a_boot_gives_a_given_up_watchdog_a_fresh_start():
    state = WatchdogState(
        start_failures=MAX_START_FAILURES, next_attempt_at=10**12, last_check=1.0,
        starts=[1.0] * MAX_STARTS_PER_WINDOW,
    )
    verdict, _, state = decide(Observation(now=2.0, database_ready=True), state, boot=True)
    assert verdict is Verdict.START
    assert state.start_failures == 0 and state.starts == []


def test_the_loop_treats_its_first_check_as_a_boot(tmp_path):
    host, clock = FakeHost(tmp_path), Clock()
    wd.save_state(tmp_path, WatchdogState(start_failures=MAX_START_FAILURES, last_check=clock.now))
    waits = iter([True])
    assert wd.run_loop(host, now=clock, wait=lambda seconds: next(waits)) == 0
    assert host.started == 1  # no confirmation, no stale give-up


def test_a_stop_recorded_while_the_watchdog_starts_is_a_stop_not_a_failure(tmp_path):
    from src.daemon_state import STOPPED_EXIT_CODE

    host, clock = FakeHost(tmp_path), Clock()

    def held_start():
        host.started += 1
        return CommandOutput(argv=("aq", "start"), returncode=STOPPED_EXIT_CODE, stdout="Not started")

    host.start_daemon = held_start
    result = wd.tick(host, now=clock, boot=True)
    assert result.verdict is Verdict.STOPPED
    assert result.state.start_failures == 0 and result.state.starts == []


def test_the_watchdog_takes_path_and_lang_from_the_install_record(tmp_path):
    record = {"path": "/opt/tools/bin:/usr/bin", "lang": "C.UTF-8", "aq": "/venv/bin/aq"}
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "install.json").write_text(json.dumps(record))
    loaded = wd.read_install_record(tmp_path)
    environ = {"PATH": "/usr/bin:/bin"}
    wd.apply_recorded_environment(loaded, environ)
    assert environ == {"PATH": "/opt/tools/bin:/usr/bin", "LANG": "C.UTF-8"}
    assert wd.read_install_record(tmp_path / "nowhere") == {}


def test_a_long_path_stays_off_the_cron_line_and_in_the_record(paths, venv, tmp_path):
    windows = [tmp_path / f"win{index:03d}" for index in range(60)]
    for directory in windows:
        directory.mkdir()
    long_path = os.pathsep.join([str(venv), *map(str, windows), "/usr/bin"])
    assert len(long_path) > 1000
    runner = Runner({("crontab", "-l"): ok("")})

    action = install_service(
        aq=str(venv / "aq"), python=str(venv / "python"), environ={"PATH": long_path},
        runner=runner, paths=paths, facts=LINUX_NO_BUS, user="operator",
    )

    assert action.ok, action.summary
    written = runner.input_for("crontab", "-")
    assert all(len(line) <= 998 for line in written.splitlines())
    assert json.loads(paths.record_path.read_text())["path"].startswith(long_path)


def test_without_linger_auto_prefers_cron_over_a_unit_that_dies_at_logout(paths, venv, tmp_path):
    linger = tmp_path / "linger"
    linger.mkdir()
    runner = Runner(
        {("crontab", "-l"): ok(""), ("loginctl",): fail("denied"), ("sudo",): fail("password")}
    )
    action = _install(paths, venv, runner, LINUX_SYSTEMD, linger_root=linger)
    assert action.ok and action.mechanism == MECHANISM_CRON
    assert not paths.unit_path.exists()
    assert any("using cron instead of systemd" in note for note in action.notes)

    explicit = _install(
        paths, venv, runner, LINUX_SYSTEMD, linger_root=linger, requested=MECHANISM_SYSTEMD
    )
    assert explicit.ok and explicit.mechanism == MECHANISM_SYSTEMD
    assert any(note.startswith("WARNING: without linger") for note in explicit.notes)


def test_without_linger_or_cron_the_user_is_asked(paths, venv, tmp_path):
    linger = tmp_path / "linger"
    linger.mkdir()
    runner = Runner({("loginctl",): fail("denied"), ("sudo",): fail("password")})
    facts = replace(LINUX_SYSTEMD, crontab=False)
    action = _install(paths, venv, runner, facts, linger_root=linger)
    assert not action.ok and action.needs_user and "enable-linger" in action.remediation
    assert not paths.unit_path.exists()


def test_a_damaged_cron_block_is_never_guessed_at(paths, venv):
    from src.install.service import CronBlockError

    damaged = f"# mine\n{CRON_BEGIN}\n@reboot old entry\n# the operator's job\n0 3 * * * backup.sh\n"
    with pytest.raises(CronBlockError):
        strip_cron_block(damaged)
    runner = Runner({("crontab", "-l"): ok(damaged)})

    action = _install(paths, venv, runner, LINUX_NO_BUS)
    assert not action.ok and "end" in action.summary.lower()
    removal = uninstall_service(runner=runner, paths=paths)
    assert not removal.ok
    assert runner.input_for("crontab", "-") is None  # never written back


def test_uninstall_plans_a_service_the_record_does_not_list(tmp_path):
    from src.install.lifecycle import plan_uninstall
    from src.install.results import ResourceRecord
    from src.install.state import InstallState

    extra = ResourceRecord(kind=RESOURCE_SERVICE, id=MECHANISM_CRON)
    plan = plan_uninstall(InstallState("", ""), extra=(extra,))
    assert [item.kind for item in plan.removals] == [RESOURCE_SERVICE]

    state = InstallState("", "")
    recorded = ResourceRecord(kind=RESOURCE_SERVICE, id=MECHANISM_SYSTEMD)
    state.resources[recorded.key] = recorded
    plan = plan_uninstall(state, extra=(extra,))
    assert [item.id for item in plan.removals] == [MECHANISM_SYSTEMD]  # not twice


def test_the_installed_service_is_found_from_its_record_or_its_files(paths):
    from src.install.service import installed_service_resource

    assert installed_service_resource(paths) is None
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("[Unit]\n")
    assert installed_service_resource(paths).id == MECHANISM_SYSTEMD
    paths.record_path.parent.mkdir(parents=True)
    paths.record_path.write_text(json.dumps({"mechanism": MECHANISM_CRON}))
    assert installed_service_resource(paths).id == MECHANISM_CRON


def test_aq_uninstall_of_another_home_leaves_this_homes_service_alone(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.install import service as service_mod

    asked = []
    monkeypatch.setattr(
        service_mod, "installed_service_resource", lambda *a, **k: asked.append(1)
    )
    result = CliRunner().invoke(
        cli, ["uninstall", "--json", "--state-file", str(tmp_path / "nothing.json")]
    )
    assert result.exit_code == 0, result.output
    assert asked == []
