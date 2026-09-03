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
    _missing_paths,
    _positional_args,
    _run_forwarding_signals,
    _xdist_disabled,
)
from src.config import ResourcesConfig

#: ``_compose_pytest_argv`` returns a full command line whose first three
#: entries are ``<python> -m pytest``.  Everything the tests care about is
#: what comes after, and slicing here keeps ``argv.index("-m")`` from
#: matching the interpreter's own ``-m``.
def _args(argv: list[str]) -> list[str]:
    return argv[3:]


@pytest.fixture
def runner():
    return CliRunner()


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
        assert "-m" not in args

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
        assert _missing_paths(
            ("tests/test_real.py", "tests/test_typo.py")
        ) == ["tests/test_typo.py"]

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

    def test_an_existing_path_still_runs(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", lambda _argv: 0)
        result = runner.invoke(cli, ["test", "tests/test_cli_test_runner.py"])
        assert result.exit_code == 0


class TestEmptyCollectionIsNotASuccess:
    def test_exit_code_five_is_explained(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(tmp_path / "config.yaml"))
        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", lambda _argv: 5)
        result = runner.invoke(cli, ["test", "tests/test_cli_test_runner.py", "-k", "nothing"])
        assert result.exit_code == 5
        assert "no tests were collected" in result.output


class TestCapResolution:
    def test_config_supplies_the_caps(self, monkeypatch):
        monkeypatch.delenv("AQ_TEST_SLOTS", raising=False)
        monkeypatch.delenv("AQ_TEST_WORKERS", raising=False)
        res = ResourcesConfig(cores=24, max_concurrent_agents=8, test_slots=3)
        slots, workers, markers, poll, timeout = _caps(res)
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
            f"database_path: {tmp_path / 'aq.db'}\n"
            "discord:\n"
            "  bot_token: test-token\n"
            "  guild_id: '1'\n"
        )
        monkeypatch.setattr("src.cli.test_runner.CONFIG_PATH", str(config_path))
        monkeypatch.setenv("AQ_TEST_SLOTS", "1")
        monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", lambda _argv: 0)

        result = runner.invoke(cli, ["test", "tests/test_config.py"])

        assert result.exit_code == 0
        assert (data_dir / "locks" / "test-slots").is_dir()
        assert not (tmp_path / "locks" / "test-slots").exists()

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
            result = runner.invoke(
                cli, ["test", "--aq-no-wait", "tests/test_config.py"]
            )
        finally:
            import os

            os.close(held[1])
        # EX_TEMPFAIL: "come back later", not "your tests failed".
        assert result.exit_code == 75
        assert "no test slot free" in result.output


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
            assert _run_forwarding_signals(
                [sys.executable, "-c", script, str(fd), str(observed)]
            ) == 0
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
        # Collection alone must not reach for a live Postgres: without a DSN
        # the perf modules still import (their skipif does the rest), and
        # AQ_REQUIRE_POSTGRES_TESTS would otherwise turn that into an error.
        env.pop("POSTGRES_TEST_DSN", None)
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
