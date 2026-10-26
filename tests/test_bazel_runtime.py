"""H5 contracts: real pytest lifecycles with synthetic time, never timing benchmarks."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.bazel.pytest_plugin import RuntimePlugin, signal_deadline
from tools.bazel.runtime import (
    TARGET_SECONDS,
    TEST_SECONDS,
    BudgetExpired,
    Deadline,
    Shard,
    flake_contract,
    normalize_nodeid,
    promotion_blockers,
    run_target,
    shard_for,
    validate_shards,
    validate_target,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pg_backend():
    """This module exercises a pure plugin; it does not allocate PostgreSQL."""


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run_pytest(
    tmp_path, source, *, fixtures="", index=None, count=None, tags=(), args=(), real=False
):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text(textwrap.dedent(source))
    (tmp_path / "conftest.py").write_text(textwrap.dedent(fixtures))
    (tmp_path / "fake_clock.py").write_text(
        "now = 0.0\ndef clock(): return now\n"
        "def advance(seconds):\n    global now\n    now += seconds\n"
    )
    env = {}
    if index is not None:
        env = {
            "TEST_SHARD_INDEX": str(index),
            "TEST_TOTAL_SHARDS": str(count),
            "TEST_SHARD_STATUS_FILE": str(tmp_path / "shard-status"),
        }
    launcher = f"""
import pytest
from pathlib import Path
from contextlib import nullcontext
from fake_clock import clock
from tools.bazel.pytest_plugin import RuntimePlugin
plugin = RuntimePlugin(output=Path('result.json'), env={env!r}, target_tags={tags!r},
    shard_isolation='synthetic isolated fixtures',
    **({{}} if {real!r} else {{'clock': clock, 'alarm': lambda d: nullcontext()}}))
raise SystemExit(pytest.main(['-c', 'pytest.ini', '--rootdir=.', '-q', 'test_sample.py',
    *{list(args)!r}], plugins=[plugin]))
"""
    result = subprocess.run(
        [sys.executable, "-c", launcher],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = tmp_path / "result.json"
    assert output.exists(), result.stdout + result.stderr
    return result, json.loads(output.read_text())


def test_normalization_preserves_parameter_identity():
    assert normalize_nodeid(r".\tests\test_x.py::test_x[a\b/../c]") == (
        r"tests/test_x.py::test_x[a\b/../c]"
    )
    assert shard_for("./tests/test_x.py::test_x", 7) == shard_for("tests/test_x.py::test_x", 7)


@pytest.mark.parametrize("node", ["/tmp/test_x.py::x", "../test_x.py::x", "C:\\test_x.py", ""])
def test_invalid_node_paths_refused(node):
    with pytest.raises(ValueError):
        normalize_nodeid(node)


def test_shards_use_stable_digest_independent_of_collection_order():
    nodes = [f"tests/test_x.py::test_value[{n}]" for n in range(100)]
    partitions = [Shard(i, 7).select(nodes) for i in range(7)]
    assert sorted(sum(partitions, [])) == sorted(nodes)
    for i in range(7):
        assert set(Shard(i, 7).select(list(reversed(nodes)))) == set(partitions[i])
    # A fixed digest vector also catches accidental algorithm changes.
    import hashlib

    expected = int(hashlib.sha256(nodes[0].encode()).hexdigest(), 16) % 7
    assert shard_for(nodes[0], 7) == expected


@pytest.mark.parametrize(
    "env",
    [
        {"TEST_SHARD_INDEX": "0"},
        {"TEST_TOTAL_SHARDS": "2"},
        {"TEST_TOTAL_SHARDS": "0", "TEST_SHARD_INDEX": "0", "TEST_SHARD_STATUS_FILE": "s"},
        {"TEST_TOTAL_SHARDS": "2", "TEST_SHARD_INDEX": "2", "TEST_SHARD_STATUS_FILE": "s"},
        {"TEST_TOTAL_SHARDS": "2", "TEST_SHARD_INDEX": "-1", "TEST_SHARD_STATUS_FILE": "s"},
        {"TEST_TOTAL_SHARDS": "2", "TEST_SHARD_INDEX": "0"},
        {"TEST_TOTAL_SHARDS": "wat", "TEST_SHARD_INDEX": "0", "TEST_SHARD_STATUS_FILE": "s"},
    ],
)
def test_malformed_shard_environment_fails(env):
    with pytest.raises(ValueError):
        Shard.from_env(env)


def test_sharding_requires_independent_reset_evidence(tmp_path):
    with pytest.raises(ValueError, match="independent"):
        RuntimePlugin(
            output=tmp_path / "r",
            env={
                "TEST_TOTAL_SHARDS": "2",
                "TEST_SHARD_INDEX": "0",
                "TEST_SHARD_STATUS_FILE": "s",
            },
        )


def test_duplicate_normalized_collection_is_refused():
    with pytest.raises(ValueError, match="duplicate"):
        Shard().select(["./test_x.py::x", "test_x.py::x"])


def test_real_pytest_shards_cover_selected_marker_suite_exactly_once(tmp_path):
    source = """
        import pytest
        @pytest.mark.parametrize('n', range(9))
        def test_value(n): assert n >= 0
        @pytest.mark.excluded
        def test_excluded(): assert False
    """
    reports = []
    for index in range(3):
        directory = tmp_path / str(index)
        result, report = run_pytest(
            directory, source, index=index, count=3, args=("-m", "not excluded")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (directory / "shard-status").exists()
        reports.append(report)
    expected = [f"test_sample.py::test_value[{n}]" for n in range(9)]
    assert validate_shards(expected, reports) == []
    assert validate_shards(expected, reports[:-1])
    assert validate_shards(expected, reports + [reports[0]])
    broken = copy.deepcopy(reports)
    next(r for r in broken if r["nodes"])["nodes"].pop()
    assert validate_shards(expected, broken)
    broken = copy.deepcopy(reports)
    next(r for r in broken if r["nodes"])["nodes"][0]["phases"].pop("teardown")
    assert validate_shards(expected, broken)
    assert validate_shards(expected + ["test_sample.py::missing"], reports)
    for mutation in ({"outcome": "failed"}, {"duration": -1}, {"duration": 61}):
        broken = copy.deepcopy(reports)
        phase = next(r for r in broken if r["nodes"])["nodes"][0]["phases"]["call"]
        phase.update(mutation)
        assert validate_shards(expected, broken)


def test_empty_partition_is_success_but_empty_collection_is_not(tmp_path):
    node = "test_sample.py::test_one"
    index = 1 - shard_for(node, 2)
    result, report = run_pytest(
        tmp_path / "empty-shard", "def test_one(): pass", index=index, count=2
    )
    assert result.returncode == 0
    assert report["nodes"] == [] and report["collected"] == [node]
    assert validate_shards([node], [report])  # missing sibling still blocks
    result, report = run_pytest(tmp_path / "empty-suite", "value = 1")
    assert result.returncode != 0
    assert "missing or empty suite collection" in report["errors"]


def test_shared_fixture_setup_is_charged_to_trigger_and_teardown_to_last(tmp_path):
    result, report = run_pytest(
        tmp_path,
        """
        from fake_clock import advance
        def test_first(shared): advance(7)
        def test_last(shared): advance(11)
    """,
        fixtures="""
        import pytest
        from fake_clock import advance
        @pytest.fixture(scope='session')
        def shared():
            advance(13)
            yield
            advance(17)
    """,
    )
    assert result.returncode == 0, result.stdout
    first, last = report["nodes"]
    assert first["elapsed"] == 20
    assert last["elapsed"] == 28
    assert first["phases"]["setup"]["duration"] == 13
    assert last["phases"]["teardown"]["duration"] == 17
    assert first["shared_setup"] == [{"fixture": "shared", "scope": "session", "duration": 13}]
    assert last["shared_setup"] == []


@pytest.mark.parametrize("setup,call,teardown", [(61, 0, 0), (20, 41, 0), (20, 20, 21)])
def test_budget_includes_all_phases_and_finalizes_even_under_xfail(tmp_path, setup, call, teardown):
    result, report = run_pytest(
        tmp_path,
        f"""
        import pytest
        from fake_clock import advance
        @pytest.mark.xfail(reason='product defect')
        def test_slow(resource): advance({call})
    """,
        fixtures=f"""
        import pytest
        from pathlib import Path
        from fake_clock import advance
        @pytest.fixture
        def resource(request):
            request.addfinalizer(lambda: Path('cleaned').touch())
            advance({setup})
            yield
            advance({teardown})
    """,
    )
    assert result.returncode == 1, result.stdout
    assert (tmp_path / "cleaned").exists()
    assert report["nodes"][0]["budget_exceeded"]
    assert report["errors"]
    assert all(p["result"] != "xfail" for p in report["nodes"][0]["phases"].values())


def test_signal_interrupt_is_failure_and_runs_finalizers(tmp_path):
    result, report = run_pytest(
        tmp_path,
        """
        import signal
        def test_interrupt(resource): signal.raise_signal(signal.SIGALRM)
    """,
        fixtures="""
        import pytest
        from pathlib import Path
        @pytest.fixture
        def resource():
            yield
            Path('cleaned').touch()
    """,
        real=True,
    )
    assert result.returncode == 1
    assert (tmp_path / "cleaned").exists()
    assert report["nodes"][0]["budget_exceeded"]


def test_signal_timer_uses_remaining_budget_and_restores_handler(monkeypatch):
    clock = Clock()
    deadline = Deadline(TEST_SECONDS, clock)
    clock.advance(23)
    calls = []
    monkeypatch.setattr(signal, "getitimer", lambda _: (0.0, 0.0))
    monkeypatch.setattr(signal, "getsignal", lambda _: "old")
    monkeypatch.setattr(signal, "signal", lambda *args: calls.append(args))
    monkeypatch.setattr(signal, "setitimer", lambda *args: calls.append(args))
    with signal_deadline(deadline):
        assert calls[1] == (signal.ITIMER_REAL, 37)
    assert calls[-2:] == [(signal.ITIMER_REAL, 0), (signal.SIGALRM, "old")]


def test_outcomes_retain_xfail_xpass_skip_errors_and_parameter_ids(tmp_path):
    result, report = run_pytest(
        tmp_path,
        """
        import pytest
        @pytest.mark.parametrize('value', [1], ids=['a/b'])
        def test_pass(value): pass
        @pytest.mark.skip(reason='unsupported')
        def test_skip(): pass
        @pytest.mark.xfail(reason='known', strict=True)
        def test_xfail(): assert False
        @pytest.mark.xfail(reason='known', strict=False)
        def test_xpass(): pass
        @pytest.mark.xfail(reason='known', strict=True)
        def test_strict_xpass(): pass
        def test_setup_error(broken): pass
        def test_teardown_error(bad_cleanup): pass
    """,
        fixtures="""
        import pytest
        @pytest.fixture
        def broken(): raise RuntimeError('setup failed')
        @pytest.fixture
        def bad_cleanup():
            yield
            raise RuntimeError('cleanup failed')
    """,
    )
    assert result.returncode == 1
    nodes = {n["nodeid"].split("::")[1]: n for n in report["nodes"]}
    assert nodes["test_pass[a/b]"]["parameter_id"] == "a/b"
    assert nodes["test_skip"]["phases"]["setup"]["result"] == "skip"
    for name, outcome, strict in [
        ("test_xfail", "xfail", True),
        ("test_xpass", "xpass", False),
        ("test_strict_xpass", "xpass", True),
    ]:
        phase = nodes[name]["phases"]["call"]
        assert (phase["result"], phase["xfail_strict"]) == (outcome, strict)
        assert phase["reason"]
    assert nodes["test_setup_error"]["phases"]["setup"]["result"] == "error"
    assert nodes["test_teardown_error"]["phases"]["teardown"]["result"] == "error"


def test_collection_failure_and_early_stop_never_certify_complete_coverage(tmp_path):
    result, report = run_pytest(tmp_path / "collection", "raise RuntimeError('bad import')")
    assert result.returncode != 0 and report["collection_errors"]
    result, report = run_pytest(
        tmp_path / "early-stop",
        """
        def test_bad(): assert False
        def test_other(): pass
    """,
        args=("-x",),
    )
    assert result.returncode != 0
    assert "selected nodes did not execute exactly once" in report["errors"]


def test_quarantine_runs_once_is_visible_and_blocks_promotion(tmp_path):
    result, report = run_pytest(
        tmp_path,
        """
        import pytest
        from pathlib import Path
        @pytest.mark.flaky(owner='team', evidence='incident:123')
        def test_flake():
            with Path('attempts').open('a') as f: f.write('attempt\\n')
    """,
        tags=("flaky", "no-cache", "external"),
    )
    assert result.returncode == 0
    assert (tmp_path / "attempts").read_text() == "attempt\n"
    assert validate_shards(["test_sample.py::test_flake"], [report]) == []
    assert promotion_blockers([report]) == [
        "unresolved flaky quarantine: test_sample.py::test_flake (team)"
    ]


@pytest.mark.parametrize(
    "owner,evidence,tags,retries",
    [
        ("", "incident", ["flaky", "no-cache", "external"], 0),
        ("owner", "", ["flaky", "no-cache", "external"], 0),
        ("owner", "incident", ["flaky", "local"], 0),
        ("owner", "incident", ["flaky", "no-cache", "external"], 1),
    ],
)
def test_invalid_flake_contract_is_rejected(owner, evidence, tags, retries):
    with pytest.raises(ValueError):
        flake_contract(owner, evidence, tags, retries)


def test_deadline_exact_boundary_and_budget_growth_contract():
    assert (TEST_SECONDS, TARGET_SECONDS) == (60, 300)
    clock = Clock()
    deadline = Deadline(60, clock)
    clock.advance(60)
    deadline.check("test")
    clock.advance(0.01)
    with pytest.raises(BudgetExpired):
        deadline.check("test")


@pytest.mark.parametrize(
    "durations,fail_phase,expected",
    [
        ((10, 20, 5), None, 0),
        ((150, 140, 10), None, 0),
        ((296, 0, 1), None, 1),
        ((100, 196, 1), None, 1),
        ((100, 190, 11), None, 1),
        ((1, 1, 1), "bootstrap", 1),
        ((1, 1, 1), "pytest", 1),
        ((1, 1, 1), "cleanup", 1),
    ],
)
async def test_target_deadline_covers_bootstrap_execution_cleanup(durations, fail_phase, expected):
    clock = Clock()
    called = []

    def operation(name, duration):
        async def run():
            called.append(name)
            clock.advance(duration)
            if fail_phase == name:
                raise RuntimeError(name)
            return 0

        return run

    report = await run_target(
        operation("bootstrap", durations[0]),
        operation("pytest", durations[1]),
        operation("cleanup", durations[2]),
        lambda: called.append("kill"),
        clock=clock,
    )
    assert report["exit_status"] == expected
    assert "cleanup" in called
    assert ("kill" in called) == bool(expected)
    assert report["elapsed"] == sum(report["phases"].values())
    if durations[0] > 295 or fail_phase == "bootstrap":
        assert "pytest" not in called


async def test_target_entry_timestamp_includes_launcher_work_before_bootstrap():
    clock = Clock()
    clock.advance(296)
    calls = []

    async def operation():
        calls.append("operation")
        return 0

    report = await run_target(
        operation, operation, operation, lambda: calls.append("kill"), clock=clock, started=0
    )
    assert report["exit_status"] == 1
    assert calls == ["operation", "kill"]  # only cleanup runs


async def test_pytest_failure_stays_failed_after_successful_cleanup():
    async def setup():
        pass

    async def execute():
        return 2

    async def cleanup():
        pass

    report = await run_target(setup, execute, cleanup, lambda: None)
    assert report["exit_status"] == 2 and report["errors"] == []


async def test_blocked_operation_is_cancelled_before_cleanup_without_wall_clock_wait(monkeypatch):
    import asyncio
    from tools.bazel import runtime

    real_wait = asyncio.wait
    calls = []

    async def fake_wait(tasks, *, timeout):
        calls.append(timeout)
        if len(calls) == 2:
            # Deterministic timeout of execution, with no real 295-second wait.
            await asyncio.sleep(0)
            return set(), tasks
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(runtime.asyncio, "wait", fake_wait)
    events = []

    async def bootstrap():
        events.append("bootstrap")

    async def execute():
        try:
            await asyncio.Future()
        finally:
            events.append("cancelled")

    async def cleanup():
        events.append("cleanup")

    report = await run_target(bootstrap, execute, cleanup, lambda: events.append("kill"))
    assert report["exit_status"] == 1
    assert events == ["bootstrap", "cancelled", "cleanup", "kill"]
    assert len(calls) == 3 and calls[0] <= 295 and calls[2] <= 300


async def test_cleanup_timeout_forces_owned_process_kill(monkeypatch):
    import asyncio
    from tools.bazel import runtime

    real_wait = asyncio.wait
    calls = 0

    async def fake_wait(tasks, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 3:
            await asyncio.sleep(0)
            return set(), tasks
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(runtime.asyncio, "wait", fake_wait)
    killed = []

    async def bootstrap():
        pass

    async def execute():
        return 0

    async def cleanup():
        await asyncio.Future()

    report = await run_target(bootstrap, execute, cleanup, lambda: killed.append(True))
    assert report["exit_status"] == 1 and killed == [True]
    assert "cleanup BudgetExpired" in report["errors"][0]


async def test_target_receipt_required_and_includes_cleanup():
    clock = Clock()

    async def phase():
        clock.advance(10)
        return 0

    report = await run_target(phase, phase, phase, lambda: None, clock=clock)
    assert validate_target(report) == []
    assert validate_target(None)
    for key, value in [
        ("elapsed", 301),
        ("elapsed", float("nan")),
        ("elapsed", 20),
        ("exit_status", 1),
        ("budget_seconds", 301),
        ("phases", {"pytest": 20}),
    ]:
        assert validate_target({**report, key: value})


def test_flaky_failure_is_not_retried_or_hidden(tmp_path):
    result, report = run_pytest(
        tmp_path,
        """
        import pytest
        from pathlib import Path
        @pytest.mark.flaky(owner='team', evidence='incident:123')
        def test_flake():
            with Path('attempts').open('a') as f: f.write('attempt\\n')
            assert False
    """,
        tags=("flaky", "no-cache", "external"),
    )
    assert result.returncode == 1
    assert (tmp_path / "attempts").read_text() == "attempt\n"
    assert promotion_blockers([report])
    assert validate_shards(["test_sample.py::test_flake"], [report])


def test_no_cache_without_always_execute_tag_is_insufficient():
    with pytest.raises(ValueError, match="external"):
        flake_contract("team", "incident", ["flaky", "no-cache"])
