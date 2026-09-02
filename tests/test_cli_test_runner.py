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
from src.cli.test_runner import _caps, _compose_pytest_argv, _has_flag, _xdist_disabled
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


class TestCapResolution:
    def test_config_supplies_the_caps(self):
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
