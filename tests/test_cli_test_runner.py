"""``aq test`` — the CLI half of resource gating layer 2.

The wrapper only earns its place if it is invisible: everything that is not
an ``--aq-*`` option has to reach pytest untouched, and a cap is added only
where the caller did not already express an intent.  A wrapper that ate
``-k`` or silently overrode ``-m perf`` would be trained around within a
day, which is how the box got saturated in the first place.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.test_runner import (
    _caps,
    _compose_pytest_argv,
    _has_flag,
    _is_full_suite,
    _missing_paths,
    _positional_args,
    _run_forwarding_signals,
    _xdist_disabled,
    postgres_test_dsn_error,
)
from src.config import ResourcesConfig


#: ``_compose_pytest_argv`` returns a full command line whose first three
#: entries are ``<python> -m pytest``.  Everything the tests care about is
#: what comes after, and slicing here keeps ``argv.index("-m")`` from
#: matching the interpreter's own ``-m``.
def _args(argv: list[str]) -> list[str]:
    return argv[3:]


@pytest.fixture
def runner(monkeypatch):
    # Command tests mock the pytest child; a non-production placeholder lets
    # them exercise the wrapper past its required-DSN preflight.
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql://test:test@localhost/postgres")
    return CliRunner()


@pytest.fixture
def isolated_test_slots(monkeypatch, tmp_path):
    # Command tests that mock the pytest child still acquire a slot. Keep
    # nested ``aq test`` invocations out of the parent run's global semaphore.
    lock_dir = tmp_path / "test-slots"
    monkeypatch.setattr("src.resources.semaphore.default_lock_dir", lambda _config: lock_dir)
    return lock_dir


class TestArgvComposition:
    def test_the_worker_cap_is_added(self):
        args = _args(
            _compose_pytest_argv(
                ("tests/test_x.py",), workers=4, markers="not perf", apply_markers=True
            )
        )
        assert args[:2] == ["-n", "4"]
        assert args[-1] == "tests/test_x.py"

    def test_an_explicit_n_is_never_doubled(self):
        args = _args(
            _compose_pytest_argv(("-n", "8", "tests/"), workers=4, markers="", apply_markers=False)
        )
        assert args.count("-n") == 1
        assert args[args.index("-n") + 1] == "8"

    @pytest.mark.parametrize("spelling", ["-n8", "--numprocesses=8", "--numprocesses"])
    def test_every_n_spelling_is_recognised(self, spelling):
        assert _has_flag((spelling, "tests/"), "-n", "--numprocesses")

    def test_p_no_xdist_suppresses_the_cap(self):
        # `-p no:xdist` is an explicit "run me serially"; adding -n on top
        # would make pytest error out.
        args = _args(
            _compose_pytest_argv(
                ("-p", "no:xdist", "tests/"), workers=4, markers="", apply_markers=False
            )
        )
        assert "-n" not in args
        # ``--dist`` is an xdist option too: with the plugin off it is
        # "unrecognized arguments", exactly like ``-n``.
        assert "--dist" not in args

    def test_the_worker_cap_distributes_by_file(self):
        # ``--dist loadfile`` keeps a module's tests on one worker (PR #47
        # chose it for the per-module database fixtures).  It used to live
        # in pyproject's addopts, where it broke ``pytest -p no:xdist``; now
        # it travels with the ``-n`` the wrapper adds.
        args = _args(_compose_pytest_argv(("tests/",), workers=4, markers="", apply_markers=False))
        assert args[args.index("--dist") + 1] == "loadfile"

    def test_an_explicit_dist_mode_is_never_doubled(self):
        args = _args(
            _compose_pytest_argv(
                ("--dist", "load", "tests/"), workers=4, markers="", apply_markers=False
            )
        )
        assert args.count("--dist") == 1
        assert args[args.index("--dist") + 1] == "load"

    def test_an_unrelated_p_flag_still_gets_the_cap(self):
        # Regression: treating any -p as "the caller manages plugins"
        # silently dropped the worker cap from every
        # `aq test ... -p no:cacheprovider`, which is the shape agents use
        # most.  -n alongside an unrelated -p is valid pytest.
        args = _args(
            _compose_pytest_argv(
                ("-p", "no:cacheprovider", "tests/"), workers=4, markers="", apply_markers=False
            )
        )
        assert args[:2] == ["-n", "4"]
        assert not _xdist_disabled(("-p", "no:cacheprovider"))
        assert _xdist_disabled(("-p", "no:xdist"))

    def test_default_marker_deselects_are_applied(self):
        args = _args(
            _compose_pytest_argv(("tests/",), workers=4, markers="not tmux", apply_markers=True)
        )
        assert args[args.index("-m") + 1] == "not tmux"

    def test_an_explicit_marker_expression_wins(self):
        args = _args(
            _compose_pytest_argv(
                ("-m", "perf", "tests/perf"), workers=4, markers="not perf", apply_markers=True
            )
        )
        assert args.count("-m") == 1
        assert args[args.index("-m") + 1] == "perf"

    def test_markers_can_be_turned_off(self):
        args = _args(
            _compose_pytest_argv(("tests/",), workers=4, markers="not tmux", apply_markers=False)
        )
        assert args[args.index("-m") + 1] == ""

    def test_pytest_args_are_passed_through_verbatim(self):
        args = ("tests/test_x.py::TestY::test_z", "-k", "not slow", "-x", "--lf")
        argv = _compose_pytest_argv(args, workers=4, markers="", apply_markers=False)
        assert argv[-len(args) :] == list(args)


class TestMissingPathsAreRefused:
    """A mistyped path must not read as a clean run.

    Under xdist, ``pytest tests/test_real.py tests/test_typo.py`` collects
    nothing and prints "no tests ran" — an agent reads that as green and
    closes its task believing it verified its change.  This happened twice
    in one session before the cause was spotted, so the check happens
    before a slot is even taken.
    """

    def test_a_missing_file_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").write_text("")
        assert _missing_paths(("tests/test_real.py",)) == []
        assert _missing_paths(("tests/test_real.py", "tests/test_typo.py")) == [
            "tests/test_typo.py"
        ]

    def test_a_node_id_suffix_is_stripped_before_the_stat(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").write_text("")
        assert _missing_paths(("tests/test_real.py::TestX::test_y",)) == []
        assert _missing_paths(("tests/test_gone.py::TestX",)) == ["tests/test_gone.py::TestX"]

    def test_a_directory_counts_as_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        assert _missing_paths(("tests/",)) == []

    def test_option_values_are_never_mistaken_for_paths(self, tmp_path, monkeypatch):
        # `-k` takes an expression, not a path; stat'ing it would refuse a
        # perfectly valid command line, which is how wrappers get worked
        # around rather than fixed.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        args = ("tests/", "-k", "schema/setup", "-m", "perf", "--ignore", "tests/nope.py", "-x")
        assert _missing_paths(args) == []

    def test_non_path_shaped_arguments_are_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _missing_paths(("tests",)) == []

    def test_positional_args_after_a_double_dash(self):
        assert _positional_args(("-x", "--", "-k", "tests/a.py")) == ["-k", "tests/a.py"]

    def test_the_command_refuses_before_taking_a_slot(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))

        def _boom(*_a, **_kw):  # pragma: no cover - must not be reached
            raise AssertionError("pytest was launched for a nonexistent path")

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _boom)
        result = runner.invoke(cli, ["test", "tests/definitely_not_a_test_file.py"])
        assert result.exit_code == 4
        assert "no such test path" in result.output

    def test_an_existing_path_still_runs(self, runner, monkeypatch, tmp_path, isolated_test_slots):
        from types import SimpleNamespace

        from src.resources.semaphore import SlotSemaphore

        # Model the parent's shared gate with its only slot occupied. The
        # nested command must use isolated_test_slots even when the shared
        # gate cannot grant another slot.
        shared_data_dir = tmp_path / "shared-data"
        shared_lock_dir = shared_data_dir / "locks" / "test-slots"
        monkeypatch.setattr(
            "src.cli.test_runner._load_config",
            lambda: SimpleNamespace(
                data_dir=str(shared_data_dir), resources=ResourcesConfig(test_slots=1)
            ),
        )
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kwargs: 0
        )
        with SlotSemaphore(shared_lock_dir, 1).acquire(timeout=0):
            result = runner.invoke(cli, ["test", "tests/test_cli_test_runner.py"])
        assert result.exit_code == 0
        assert isolated_test_slots.is_dir()


class TestEmptyCollectionIsNotASuccess:
    def test_exit_code_five_is_explained(self, runner, monkeypatch, tmp_path, isolated_test_slots):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kwargs: 5
        )
        result = runner.invoke(cli, ["test", "tests/test_cli_test_runner.py", "-k", "nothing"])
        assert result.exit_code == 5
        assert "no tests were collected" in result.output


class TestCapResolution:
    def test_config_supplies_the_caps(self, monkeypatch):
        monkeypatch.delenv("AQ_TEST_SLOTS", raising=False)
        monkeypatch.delenv("AQ_TEST_WORKERS", raising=False)
        res = ResourcesConfig(cores=24, max_concurrent_agents=8, test_slots=3)
        slots, workers, markers, _poll, _timeout = _caps(res)
        assert (slots, workers) == (3, 3)
        assert "not perf" in markers

    def test_session_env_wins_over_config(self, monkeypatch):
        # The daemon derived these at launch and they are readable from a
        # worktree whose config the CLI may not be able to open.
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")
        monkeypatch.setenv("AQ_TEST_WORKERS", "2")
        slots, workers, _, _, _ = _caps(ResourcesConfig(test_slots=9, test_workers=9))
        assert (slots, workers) == (1, 2)

    def test_no_config_still_gates(self, monkeypatch):
        monkeypatch.delenv("AQ_TEST_SLOTS", raising=False)
        monkeypatch.delenv("AQ_TEST_WORKERS", raising=False)
        slots, workers, markers, _, _ = _caps(None)
        assert slots >= 1 and workers >= 1 and markers

    def test_a_junk_env_value_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AQ_TEST_WORKERS", "banana")
        _, workers, _, _, _ = _caps(ResourcesConfig(test_workers=5))
        assert workers == 5


class TestCommand:
    def test_missing_dsn_fails_once_before_taking_a_slot(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.delenv("POSTGRES_TEST_DSN")

        def _boom(*_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("pytest launched without its required DSN")

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _boom)
        result = runner.invoke(cli, ["test", "tests/test_config.py"])

        assert result.exit_code == 4
        assert result.output.count("POSTGRES_TEST_DSN is not set") == 1
        assert "docker compose up -d postgres" in result.output
        assert "Nothing was run" in result.output
        assert "aq test: slot" not in result.output

    def test_present_dsn_has_no_preflight_error(self):
        assert postgres_test_dsn_error({"POSTGRES_TEST_DSN": "postgresql://host/base"}) is None

    def test_bare_pytest_aborts_collection_once_when_dsn_is_missing(self):
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env.pop("POSTGRES_TEST_DSN", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:xdist",
                "--co",
                "-q",
                "tests/test_config.py",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 4
        assert output.count("POSTGRES_TEST_DSN is not set") == 1
        assert "docker compose up -d postgres" in output

    def test_dry_run_prints_the_command(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("AQ_TEST_WORKERS", "4")
        result = runner.invoke(cli, ["test", "--aq-dry-run", "tests/test_config.py"])
        assert result.exit_code == 0
        assert "-n 4" in result.output
        assert "tests/test_config.py" in result.output

    def test_no_arguments_refuses_rather_than_running_everything(self, runner):
        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 2
        assert "No pytest arguments" in result.output

    def test_status_renders_without_a_daemon(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        result = runner.invoke(cli, ["test", "--aq-status"])
        assert result.exit_code == 0
        assert "Test slots" in result.output

    def test_custom_data_dir_owns_the_semaphore(self, runner, monkeypatch, tmp_path):
        config_path = tmp_path / "config.yaml"
        data_dir = tmp_path / "custom-data"
        config_path.write_text(
            f"data_dir: {data_dir}\n"
            "database:\n  url: postgresql+asyncpg://localhost/aq_test\n"
            "discord:\n"
            "  bot_token: test-token\n"
            "  guild_id: '1'\n"
        )
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(config_path))
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kwargs: 0
        )

        result = runner.invoke(cli, ["test", "tests/test_config.py"])

        assert result.exit_code == 0
        assert (data_dir / "locks" / "test-slots").is_dir()
        assert not (tmp_path / "locks" / "test-slots").exists()

    def test_each_invocation_passes_a_fresh_database_ownership_token(
        self, runner, monkeypatch, tmp_path, isolated_test_slots
    ):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setenv("AQ_DB_SCOPE", "worker")
        monkeypatch.setenv("AQ_DATABASE_URL", "refuse-worker-database")
        monkeypatch.setenv("AGENT_QUEUE_DB", "refuse-worker-database")
        seen: list[dict[str, str]] = []

        def _capture(_argv, *, env):
            seen.append(env)
            return 0

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _capture)

        assert runner.invoke(cli, ["test", "tests/test_config.py"]).exit_code == 0
        assert runner.invoke(cli, ["test", "tests/test_config.py"]).exit_code == 0
        assert len(seen) == 2
        assert seen[0]["AQ_TEST_RUN_ID"] != seen[1]["AQ_TEST_RUN_ID"]
        for child_env in seen:
            assert child_env["AQ_DB_SCOPE"] == "worker"
            assert child_env["AQ_DATABASE_URL"] == "refuse-worker-database"
            assert child_env["AGENT_QUEUE_DB"] == "refuse-worker-database"

    def test_a_full_box_fails_retryably_rather_than_hanging(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")
        from src.resources.semaphore import SlotSemaphore

        lock_dir = tmp_path / "locks" / "test-slots"
        monkeypatch.setattr(
            "src.resources.semaphore.default_lock_dir",
            lambda config=None, **kw: lock_dir,
        )
        held = SlotSemaphore(lock_dir, 1).try_acquire({"task_id": "someone-else"})
        assert held is not None
        try:
            result = runner.invoke(cli, ["test", "--aq-no-wait", "tests/test_config.py"])
        finally:
            import os

            os.close(held[1])
        # EX_TEMPFAIL: "come back later", not "your tests failed".
        assert result.exit_code == 75
        assert "no test slot free" in result.output


class TestSlotReport:
    """``$AQ_TEST_SLOT_REPORT`` tells a supervising caller how long it queued.

    The development publisher bounds its validation with ``timeout_seconds``;
    without this report it charged the wait for a test slot to the test run.
    """

    @pytest.fixture(autouse=True)
    def _one_private_slot(self, monkeypatch, tmp_path, isolated_test_slots):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")

    def test_an_immediate_slot_is_reported_acquired_then_released(
        self, runner, monkeypatch, tmp_path
    ):
        import json

        report = tmp_path / "slot.jsonl"
        monkeypatch.setenv("AQ_TEST_SLOT_REPORT", str(report))
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kwargs: 0
        )
        result = runner.invoke(cli, ["test", "tests/test_config.py"])
        assert result.exit_code == 0
        events = [json.loads(line) for line in report.read_text().splitlines()]
        assert [e["event"] for e in events] == ["acquired", "released"]
        assert events[0]["waited"] >= 0 and events[0]["slot"] == 0

    def test_a_slot_timeout_is_reported_and_the_wait_env_bounds_it(
        self, runner, monkeypatch, tmp_path, isolated_test_slots
    ):
        import os

        from src.resources.semaphore import SlotSemaphore
        from src.resources.slot_report import read_slot_wait

        lock_dir = isolated_test_slots
        report = tmp_path / "slot.jsonl"
        monkeypatch.setenv("AQ_TEST_SLOT_REPORT", str(report))
        # One second, not the config's half hour: the supervising caller
        # budgets queueing on its own and must be able to say so.
        monkeypatch.setenv("AQ_TEST_WAIT_TIMEOUT", "1")
        # Config says 15 s; the env's 1 s must win.
        monkeypatch.setattr("src.cli.test_runner._caps", lambda _res: (1, 1, "", 0.05, 15))
        held = SlotSemaphore(lock_dir, 1).try_acquire({"task_id": "someone-else"})
        assert held is not None
        try:
            result = runner.invoke(cli, ["test", "tests/test_config.py"])
        finally:
            os.close(held[1])
        assert result.exit_code == 75
        wait = read_slot_wait(report)
        assert wait.timed_out
        assert 1.0 <= wait.total_seconds < 10.0
        assert wait.waiting_seconds == 0.0

    def test_no_report_is_written_unless_asked(self, runner, monkeypatch, tmp_path):
        monkeypatch.delenv("AQ_TEST_SLOT_REPORT", raising=False)
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kwargs: 0
        )
        assert runner.invoke(cli, ["test", "tests/test_config.py"]).exit_code == 0
        assert not list(tmp_path.glob("*.jsonl"))

    def test_the_wait_env_overrides_config_but_not_the_flag(self, monkeypatch):
        from src.cli.test_runner import _slot_wait_timeout

        monkeypatch.setenv("AQ_TEST_WAIT_TIMEOUT", "120")
        assert _slot_wait_timeout(1800, None) == 120
        assert _slot_wait_timeout(1800, 5) == 5
        monkeypatch.setenv("AQ_TEST_WAIT_TIMEOUT", "banana")
        assert _slot_wait_timeout(1800, None) == 1800
        monkeypatch.delenv("AQ_TEST_WAIT_TIMEOUT")
        assert _slot_wait_timeout(1800, None) == 1800


class TestSignalForwarding:
    def test_inheritable_slot_fd_reaches_the_pytest_process(self, tmp_path):
        import os
        import sys

        from src.resources.semaphore import SlotSemaphore

        acquired = SlotSemaphore(tmp_path / "slots", 1).try_acquire()
        assert acquired is not None
        _, fd = acquired
        observed = tmp_path / "inherited.txt"
        script = (
            "import os, pathlib, sys; "
            "fd = int(sys.argv[1]); "
            "path = pathlib.Path(sys.argv[2]); "
            "\ntry: os.fstat(fd)\n"
            "except OSError: path.write_text('closed')\n"
            "else: path.write_text('inherited')\n"
        )
        try:
            assert (
                _run_forwarding_signals([sys.executable, "-c", script, str(fd), str(observed)]) == 0
            )
        finally:
            os.close(fd)

        assert observed.read_text() == "inherited"


class TestDefaultDeselectsMatchPyproject:
    """The ``-m`` ``aq test`` adds must deselect what pyproject's addopts does.

    pytest's ``-m`` is single-valued: a command-line expression *replaces*
    the one in ``addopts`` rather than combining with it.  So when
    pyproject.toml grows a new deselected marker (``migration`` and ``slow``
    in PR #48) and the ``aq test`` default is not updated, every agent that
    follows the rules and runs ``aq test`` silently gets those suites back —
    the exact suites that were pulled out of the default run for cost.
    """

    @staticmethod
    def _terms(expression: str) -> set[str]:
        # "not a and not b" -> {"a", "b"}; anything else is a shape we do
        # not expect and should fail loudly.
        terms = set()
        for clause in expression.split(" and "):
            words = clause.split()
            assert words[0] == "not" and len(words) == 2, expression
            terms.add(words[1])
        return terms

    @pytest.fixture(scope="class")
    def pyproject_deselects(self) -> set[str]:
        import shlex
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())
        addopts = shlex.split(data["tool"]["pytest"]["ini_options"]["addopts"])
        return self._terms(addopts[addopts.index("-m") + 1])

    def test_config_default_matches(self, pyproject_deselects):
        assert self._terms(ResourcesConfig().test_deselect_markers) == pyproject_deselects

    def test_fallback_matches(self, pyproject_deselects):
        from src.cli.test_runner import _FALLBACK_MARKERS

        assert self._terms(_FALLBACK_MARKERS) == pyproject_deselects


class TestPyprojectKeepsSerialPytestWorking:
    """pyproject's ``addopts`` must not carry xdist options.

    PR #47 put ``-n auto --dist loadfile`` there.  ``-p no:xdist`` unloads
    the plugin *and* its options, so the documented serial path
    (``pytest -p no:xdist``, and ``aq test -p no:xdist`` which deliberately
    adds no ``-n``) died with "unrecognized arguments: -n --dist loadfile".
    Parallelism belongs on the command line: ``aq test`` adds the box's cap
    and CI passes ``-n auto`` explicitly.
    """

    def test_addopts_carry_no_xdist_options(self):
        import shlex
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())
        addopts = shlex.split(data["tool"]["pytest"]["ini_options"]["addopts"])
        assert not _has_flag(tuple(addopts), "-n", "--numprocesses", "--dist"), addopts

    def test_pytest_with_xdist_disabled_collects(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:xdist", "--co", "-q", __file__],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestPerfSuiteStaysOutOfTheDefaultRun:
    """Nothing under ``tests/perf/`` may escape the ``perf`` marker.

    The marker is what routes these suites into CI's ``postgres-integration``
    job (``-m "integration or perf"``) and out of ``Tests (default)``, which
    runs ``-n auto --dist loadfile`` — i.e. with every core busy.  A
    latency budget measured on a saturated box fails on load, not on a
    regression.

    ``tests/perf/test_layout_api_statements.py`` and
    ``test_layout_statements.py`` both shipped with
    ``pytestmark = pytest.mark.skipif(...)``, which *replaced* rather than
    added to the marker.  The consequence was invisible until it wasn't:
    the tiles p95 budget ran in the default job on every push and started
    flaking there, while never running in the job built for it.  A module
    that forgets the marker again should fail here instead.
    """

    def test_no_unmarked_tests_under_tests_perf(self):
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        # Perf's conftest resolves its run-owned database at import time, so
        # preserve this parent test run's disposable server configuration.
        assert env.get("POSTGRES_TEST_DSN")
        env.pop("AQ_REQUIRE_POSTGRES_TESTS", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:xdist",
                "--co",
                "-q",
                "-m",
                "not perf",
                "tests/perf",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        # pytest exits 5 when the run collected nothing, which is the whole
        # assertion: `-m "not perf"` is what CI's default job selects.
        assert proc.returncode == 5, (
            "tests under tests/perf/ are selected by the default suite's "
            f"-m 'not perf'; add pytest.mark.perf to their pytestmark\n"
            f"{proc.stdout}{proc.stderr}"
        )


def _fake_project(root, *, modules: int = 10, subpackage: int = 2):
    """A project whose pytest ``testpaths`` hold ``modules + subpackage`` test modules."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    tests = root / "tests"
    (tests / "sub").mkdir(parents=True)
    (tests / "conftest.py").write_text("")
    for index in range(modules):
        (tests / f"test_m{index}.py").write_text("")
    for index in range(subpackage):
        (tests / "sub" / f"test_s{index}.py").write_text("")
    return root


class TestFullSuiteClassification:
    """Which invocations count as "the whole suite".

    Full-suite runs are the ones that held three of four slots for over an
    hour on 2026-09-24; a slice of the suite, however it is spelled, must
    never be made to queue behind one.
    """

    @pytest.fixture
    def project(self, tmp_path):
        return _fake_project(tmp_path)

    @pytest.mark.parametrize(
        "args",
        [
            ("tests/",),
            ("tests",),
            (".",),
            ("-x",),  # no path: pytest collects testpaths
            ("tests/", "-x", "--tb", "short", "-p", "no:cacheprovider"),
            ("tests/", "-k", "not slow"),  # everything but a slice
            ("tests/", "-knot slow and not flaky"),
            ("-m", "", "tests/"),  # --aq-all-markers' spelling: no filter
            ("-m", "not perf", "tests"),
            ("--lf", "tests/"),  # nothing recorded: pytest runs everything
        ],
    )
    def test_whole_suite_selections(self, project, args):
        assert _is_full_suite(args, cwd=project)

    @pytest.mark.parametrize(
        "args",
        [
            ("tests/test_m0.py",),
            ("tests/sub",),
            ("tests/test_m0.py::test_x",),
            ("tests/", "-k", "claim"),  # the documented "slice of the suite"
            ("tests/", "-kclaim or pools"),
            ("tests/", "-k=claim"),
            ("-m", "perf", "tests/"),
            ("--co", "tests/"),
            ("--collect-only", "-q"),
            ("tests/", "-k", "unbalanced ("),  # pytest refuses it before running
        ],
    )
    def test_slices_are_focused(self, project, args):
        assert not _is_full_suite(args, cwd=project)

    def test_a_shell_expanded_glob_of_most_modules_is_the_full_suite(self, project):
        every_top_level_module = tuple(f"tests/test_m{i}.py" for i in range(10))
        assert _is_full_suite(every_top_level_module, cwd=project)
        an_area = tuple(f"tests/test_m{i}.py" for i in range(3))
        assert not _is_full_suite(an_area, cwd=project)

    def test_node_ids_never_count_toward_coverage(self, project):
        one_test_per_module = tuple(f"tests/test_m{i}.py::test_x" for i in range(10))
        assert not _is_full_suite(one_test_per_module, cwd=project)

    def test_last_failed_with_failures_on_record_is_a_slice(self, project):
        cache = project / ".pytest_cache" / "v" / "cache"
        cache.mkdir(parents=True)
        (cache / "lastfailed").write_text('{"tests/test_m0.py::test_x": true}')
        assert not _is_full_suite(("--lf",), cwd=project)
        (cache / "lastfailed").write_text("{}")
        assert _is_full_suite(("--lf",), cwd=project)

    def test_a_bare_run_below_the_rootdir_collects_only_that_directory(self, project):
        assert not _is_full_suite(("-x",), cwd=project / "tests" / "sub")
        assert _is_full_suite(("-x",), cwd=project / "tests")

    def test_without_pytest_config_only_the_whole_rootdir_counts(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        assert _is_full_suite((".",), cwd=tmp_path)
        assert not _is_full_suite(("pkg",), cwd=tmp_path)

    def test_this_repository(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        assert _is_full_suite(("tests/",), cwd=root)
        assert not _is_full_suite(("tests/test_cli_test_runner.py",), cwd=root)
        assert not _is_full_suite(("tests/", "-k", "schema_setup or run_schema"), cwd=root)


def _flat(output: str) -> str:
    """CLI output with Rich's 80-column wrapping undone."""
    return " ".join(output.split())


def _hold(lock_dir, slots: int, **meta):
    """Take a slot of the semaphore at *lock_dir* as someone else would."""
    from src.resources.semaphore import SlotSemaphore

    held = SlotSemaphore(lock_dir, slots).try_acquire(meta)
    assert held is not None
    return held[1]


class TestOneFullSuiteRunAtATime:
    @pytest.fixture
    def box(self, runner, monkeypatch, tmp_path):
        """A fake project as cwd, private locks, 2 slots and a fast poll."""
        from src.resources.semaphore import SlotSemaphore, full_suite_lock_dir

        project = _fake_project(tmp_path / "project")
        monkeypatch.chdir(project)
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        lock_dir = tmp_path / "locks" / "test-slots"
        monkeypatch.setattr("src.resources.semaphore.default_lock_dir", lambda _config: lock_dir)
        monkeypatch.setattr("src.cli.test_runner._caps", lambda _res: (2, 1, "", 0.05, 30))

        class Box:
            slots_dir = lock_dir
            full_dir = full_suite_lock_dir(lock_dir)
            slots = SlotSemaphore(lock_dir, 2)
            full = SlotSemaphore(full_suite_lock_dir(lock_dir), 1)

        return Box

    def test_a_second_full_run_refuses_immediately_naming_the_first(self, runner, monkeypatch, box):
        import os
        import time

        def _boom(*_a, **_kw):  # pragma: no cover - must not be reached
            raise AssertionError("a second full-suite run was launched")

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _boom)
        fd = _hold(box.full_dir, 1, task_id="first-run", cwd="/w/slot-9", since=time.time() - 3725)
        try:
            result = runner.invoke(cli, ["test", "--aq-no-wait", "tests/"])
            # Refused without taking a normal slot either.
            assert box.slots.snapshot()["free"] == 2
        finally:
            os.close(fd)
        assert result.exit_code == 75
        output = _flat(result.output)
        assert "full-suite run is already in progress" in output
        for fact in ("task first-run", "cwd /w/slot-9", "running 1h02m", "without --aq-no-wait"):
            assert fact in output

    def test_a_second_full_run_that_times_out_says_so(self, runner, monkeypatch, box):
        import os

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kw: 0)
        fd = _hold(box.full_dir, 1, task_id="first-run", cwd="/w/slot-9")
        try:
            result = runner.invoke(cli, ["test", "--aq-timeout", "0", "tests/"])
        finally:
            os.close(fd)
        assert result.exit_code == 75
        assert "for the full-suite lock; it is still held: task first-run" in _flat(result.output)

    def test_a_second_full_run_queues_without_holding_a_slot(self, runner, monkeypatch, box):
        import os
        import threading
        import time

        fd = _hold(box.full_dir, 1, task_id="first-run", cwd="/w/slot-9")
        observed: dict = {}

        def _first_run_finishes():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if list((box.full_dir / "waiters").glob("*.json")):
                    # The second full run is queued: it must not be sitting
                    # on a slot a focused run could use meanwhile.
                    observed["free_slots_while_queued"] = box.slots.snapshot()["free"]
                    break
                time.sleep(0.02)
            os.close(fd)

        during: list = []

        def _pytest(_argv, **_kw):
            during.append((box.full.snapshot(), box.slots.snapshot()))
            return 0

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _pytest)
        releaser = threading.Thread(target=_first_run_finishes)
        releaser.start()
        try:
            result = runner.invoke(cli, ["test", "tests/"])
        finally:
            releaser.join(timeout=30)
        assert result.exit_code == 0, result.output
        assert "for the full-suite lock (one full-suite run at a time)" in _flat(result.output)
        assert observed["free_slots_while_queued"] == 2
        full, slots = during[0]
        assert full["slots"][0]["held"]
        assert full["slots"][0]["holder"]["scope"] == "full"
        assert slots["free"] == 1

    def test_queueing_on_the_full_suite_lock_is_reported_as_slot_wait(
        self, runner, monkeypatch, box, tmp_path
    ):
        """The development publisher charges its run budget for running only.

        Time spent behind another full suite is queueing like a slot wait,
        so ``$AQ_TEST_SLOT_REPORT`` must count it.
        """
        import json
        import os
        import threading
        import time

        from src.resources.slot_report import read_slot_wait

        report = tmp_path / "slot.jsonl"
        monkeypatch.setenv("AQ_TEST_SLOT_REPORT", str(report))
        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", lambda _argv, **_kw: 0)
        fd = _hold(box.full_dir, 1, task_id="first-run")

        def _first_run_finishes():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if list((box.full_dir / "waiters").glob("*.json")):
                    break
                time.sleep(0.02)
            time.sleep(0.3)
            os.close(fd)

        releaser = threading.Thread(target=_first_run_finishes)
        releaser.start()
        try:
            result = runner.invoke(cli, ["test", "tests/"])
        finally:
            releaser.join(timeout=30)
        assert result.exit_code == 0, result.output
        events = [json.loads(line) for line in report.read_text().splitlines()]
        assert [e["event"] for e in events] == ["waiting", "acquired", "released"]
        assert events[1]["waited"] >= 0.3
        wait = read_slot_wait(report)
        assert wait.acquired == 1 and not wait.timed_out
        assert wait.total_seconds >= 0.3

    def test_a_full_suite_lock_timeout_is_reported_and_the_wait_env_bounds_it(
        self, runner, monkeypatch, box, tmp_path
    ):
        import os

        from src.resources.slot_report import read_slot_wait

        def _boom(*_a, **_kw):  # pragma: no cover - must not be reached
            raise AssertionError("a second full-suite run was launched")

        report = tmp_path / "slot.jsonl"
        monkeypatch.setenv("AQ_TEST_SLOT_REPORT", str(report))
        # The box's config says 30 s; the supervising caller's 1 s must win
        # for the full-suite lock exactly as it does for a slot.
        monkeypatch.setenv("AQ_TEST_WAIT_TIMEOUT", "1")
        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _boom)
        fd = _hold(box.full_dir, 1, task_id="first-run")
        try:
            result = runner.invoke(cli, ["test", "tests/"])
        finally:
            os.close(fd)
        assert result.exit_code == 75, result.output
        wait = read_slot_wait(report)
        assert wait.timed_out and wait.acquired == 0
        assert 1.0 <= wait.total_seconds < 10.0
        assert wait.waiting_seconds == 0.0

    def test_a_full_run_takes_the_lock_and_exactly_one_slot(self, runner, monkeypatch, box):
        during: list = []

        def _pytest(_argv, **_kw):
            during.append((box.full.snapshot(), box.slots.snapshot()))
            return 0

        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _pytest)
        result = runner.invoke(cli, ["test", "tests/"])
        assert result.exit_code == 0, result.output
        assert "full-suite lock + slot 0 of 2" in result.output
        full, slots = during[0]
        assert full["free"] == 0
        assert slots["free"] == 1
        assert slots["slots"][0]["holder"]["scope"] == "full"
        # Both come back when the run ends.
        assert box.full.snapshot()["free"] == 1
        assert box.slots.snapshot()["free"] == 2

    def test_focused_runs_never_wait_behind_a_full_run(self, runner, monkeypatch, box):
        import os

        launched: list = []
        monkeypatch.setattr(
            "src.cli.test_runner._run_forwarding_signals",
            lambda argv, **_kw: launched.append(argv) or 0,
        )
        full_fd = _hold(box.full_dir, 1, task_id="first-run", scope="full")
        slot_fd = _hold(box.slots_dir, 2, task_id="first-run", scope="full")
        try:
            result = runner.invoke(cli, ["test", "--aq-no-wait", "tests/test_m0.py"])
            area = runner.invoke(cli, ["test", "--aq-no-wait", "tests/", "-k", "claim"])
        finally:
            os.close(slot_fd)
            os.close(full_fd)
        assert result.exit_code == 0, result.output
        assert area.exit_code == 0, area.output
        assert len(launched) == 2
        assert "full-suite lock" not in result.output

    def test_status_shows_the_full_suite_holder_separately(self, runner, box):
        import os

        free = runner.invoke(cli, ["test", "--aq-status"])
        assert free.exit_code == 0, free.output
        assert "Full-suite lock: free" in _flat(free.output)

        full_fd = _hold(box.full_dir, 1, task_id="first-run", cwd="/w/slot-9", scope="full")
        slot_fd = _hold(box.slots_dir, 2, task_id="first-run", scope="full")
        try:
            held = runner.invoke(cli, ["test", "--aq-status"])
        finally:
            os.close(slot_fd)
            os.close(full_fd)
        assert held.exit_code == 0, held.output
        assert "Full-suite lock: held by task first-run, cwd /w/slot-9" in _flat(held.output)
        assert "busy (full suite)" in _flat(held.output)


class TestFullSuiteLockReleasedOnSignals:
    """The full-suite lock is an ``flock`` like the slots: death releases it.

    Driven through a real ``aq test`` process, because the property that
    matters is the wrapper's: SIGTERM is forwarded to pytest and both locks
    come back, and a SIGKILLed wrapper leaves the lock with the still-running
    pytest (a runaway stays accounted for) until that dies too.
    """

    _SCRIPT = """
import sys
sys.path.insert(0, {root!r})
from src.cli.app import cli
import src.cli.test_runner as runner
runner._compose_pytest_argv = lambda args, **kw: [
    sys.executable, "-c", "import time; print('pytest-started', flush=True); time.sleep(120)"
]
cli(["test", "tests/"], prog_name="aq")
"""

    @pytest.fixture
    def wrapper(self, tmp_path):
        import os
        import signal
        import subprocess
        import sys
        import threading
        from pathlib import Path

        from src.resources.semaphore import SlotSemaphore, full_suite_lock_dir

        root = Path(__file__).resolve().parent.parent
        project = _fake_project(tmp_path / "project")
        home = tmp_path / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AQ_", "AGENT_QUEUE"))}
        # A private HOME puts the default lock dir (and the config it would
        # read) under tmp_path, away from this box's real test slots.
        env.update(
            HOME=str(home),
            AQ_TEST_SLOTS="2",
            AQ_TASK_ID="signalled-run",
            POSTGRES_TEST_DSN="postgresql://test:test@localhost/postgres",
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", self._SCRIPT.format(root=str(root))],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        def _kill_group():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        watchdog = threading.Timer(90, _kill_group)
        watchdog.start()
        seen = []
        for line in proc.stdout:
            seen.append(line)
            if "pytest-started" in line:
                break
        else:
            watchdog.cancel()
            pytest.fail(f"aq test never started pytest:\n{''.join(seen)}{proc.stderr.read()}")
        lock_dir = home / ".agent-queue" / "locks" / "test-slots"
        full = SlotSemaphore(full_suite_lock_dir(lock_dir), 1)
        slots = SlotSemaphore(lock_dir, 2)
        assert full.snapshot()["slots"][0]["holder"]["task_id"] == "signalled-run"
        assert slots.snapshot()["free"] == 1
        try:
            yield proc, full, slots
        finally:
            watchdog.cancel()
            _kill_group()
            proc.wait(timeout=30)

    def test_sigterm_releases_the_lock_and_the_slot(self, wrapper):
        import signal

        proc, full, slots = wrapper
        proc.send_signal(signal.SIGTERM)  # the wrapper only; it forwards to pytest
        proc.wait(timeout=30)
        assert full.snapshot()["free"] == 1
        assert slots.snapshot()["free"] == 2

    def test_a_killed_wrapper_leaves_the_lock_with_pytest_until_it_dies(self, wrapper):
        import os
        import signal
        import time

        proc, full, slots = wrapper
        proc.kill()  # SIGKILL: nothing is forwarded
        proc.wait(timeout=30)
        assert full.snapshot()["free"] == 0, "the orphaned pytest still runs the full suite"
        os.killpg(proc.pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while full.snapshot()["free"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert full.snapshot()["free"] == 1
        assert slots.snapshot()["free"] == 2
