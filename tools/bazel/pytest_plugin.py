"""Explicitly installed pytest plugin for the Bazel runner (no global autoload).

B1: pytest.main(args, plugins=[RuntimePlugin(output=..., env=child_env, ...)]).
Run serially with pytest-timeout/retry plugins disabled: this plugin owns SIGALRM.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.bazel.runtime import (
    SCHEMA_VERSION,
    TEST_SECONDS,
    BudgetExpired,
    Deadline,
    Shard,
    flake_contract,
    normalize_nodeid,
)


def identity_properties(item, report):
    """Properties survive xdist transport and pytest's JUnit finalization."""
    marker = item.get_closest_marker("xfail")
    strict = (
        bool(marker.kwargs.get("strict", item.config.getini("xfail_strict"))) if marker else None
    )
    if marker is None and hasattr(report, "wasxfail"):
        strict = False  # imperative pytest.xfail has no unexpected-pass branch
    values = {
        "aq_nodeid": normalize_nodeid(item.nodeid),
        "aq_parameter_id": json.dumps(getattr(getattr(item, "callspec", None), "id", None)),
        "aq_xfail_strict": json.dumps(strict),
    }
    report.user_properties = [
        (key, value) for key, value in report.user_properties if key not in values
    ] + list(values.items())


def phase_result(report, strict):
    reason = str(report.longrepr) if report.longrepr else None
    if report.skipped and isinstance(report.longrepr, tuple):
        reason = str(report.longrepr[2])  # skip location is not the skip reason
    if hasattr(report, "wasxfail"):
        result = "xfail" if report.skipped else "xpass"
        reason = str(report.wasxfail)
    elif report.failed and str(report.longrepr).startswith("[XPASS(strict)]"):
        result, strict = "xpass", True
    elif report.failed:
        result = "fail" if report.when == "call" else "error"
    else:
        result = "skip" if report.skipped else "pass"
    return {
        "duration": report.duration,
        "outcome": report.outcome,
        "result": result,
        "reason": reason,
        "xfail_strict": strict,
    }


def node_outcome(phases):
    results = {phase["result"] for phase in phases.values()}
    return next(
        (
            value
            for value in ("error", "fail", "xpass", "xfail", "skip", "pass")
            if value in results
        ),
        "error",
    )


def pytest_addoption(parser):
    parser.addoption("--aq-outcomes", help="Write canonical parity outcomes (no execution policy)")


def pytest_configure(config):
    output = config.getoption("--aq-outcomes")
    if output:
        config.pluginmanager.register(OutcomePlugin(Path(output)), "aq-parity-outcomes")


class OutcomePlugin:
    """Legacy recorder: -p tools.bazel.pytest_plugin --aq-outcomes FILE.

    Does not alter markers, selection, budgets, retries or parallelism. Only the
    controller writes; workers carry parameter/strictness metadata in reports.
    The comparator rejects retries, partial runs and collection-only evidence.
    """

    def __init__(self, output):
        self.output = output
        self.collected = None
        self.nodes = []
        self.active = {}
        self.errors = []
        self.collection_errors = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        self.collected = [normalize_nodeid(item.nodeid) for item in session.items]

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        ids = [normalize_nodeid(identity) for identity in ids]
        if self.collected is not None and self.collected != ids:
            self.errors.append("xdist workers disagree on collection")
        self.collected = ids

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        identity_properties(item, report)
        return report

    def pytest_runtest_logreport(self, report):
        properties = dict(report.user_properties)
        nodeid = normalize_nodeid(report.nodeid)
        if properties.get("aq_nodeid") != nodeid:
            self.errors.append(f"missing canonical report identity: {nodeid}")
            return
        if report.when == "setup":
            node = {
                "nodeid": nodeid,
                "parameter_id": json.loads(properties["aq_parameter_id"]),
                "phases": {},
            }
            self.nodes.append(node)
            self.active[nodeid] = node
        node = self.active.get(nodeid)
        if node is None or report.when in node["phases"]:
            self.errors.append(f"duplicate or orphan phase: {nodeid} {report.when}")
            return
        node["phases"][report.when] = phase_result(
            report, json.loads(properties["aq_xfail_strict"])
        )
        node["outcome"] = node_outcome(node["phases"])

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_errors.append(
                {
                    "nodeid": normalize_nodeid(report.nodeid) if report.nodeid else "",
                    "outcome": report.outcome,
                    "reason": str(report.longrepr),
                }
            )

    def pytest_internalerror(self, excrepr, excinfo):
        self.errors.append(str(excrepr))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if hasattr(session.config, "workerinput"):
            return
        if self.collected is None or not self.collected:
            self.errors.append("missing or empty suite collection")
        if session.config.option.collectonly:
            self.errors.append("collection-only does not verify execution")
        if Counter(node["nodeid"] for node in self.nodes) != Counter(self.collected or []):
            self.errors.append("selected nodes did not execute exactly once")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "shard": {"index": 0, "count": 1},
            "collected": self.collected or [],
            "selected": self.collected or [],
            "nodes": sorted(self.nodes, key=lambda node: node["nodeid"]),
            "errors": self.errors,
            "collection_errors": self.collection_errors,
            "exit_status": int(session.exitstatus),
        }
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.output)


@contextmanager
def signal_deadline(deadline: Deadline):
    """Interrupt a hung phase; pytest still runs fixture finalizers on failure."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Bazel pytest deadlines require the main thread")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("another timer owns SIGALRM; disable pytest-timeout")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise BudgetExpired(f"test exceeded {TEST_SECONDS:g}s including fixture phases")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(deadline.remaining, 0.000001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class RuntimePlugin:
    def __init__(
        self,
        *,
        output: Path,
        env: Mapping[str, str],
        target_tags: Sequence[str] = (),
        shard_isolation: str = "",
        clock: Callable[[], float] = time.monotonic,
        alarm: Callable = signal_deadline,
    ):
        self.output = output
        self.shard = Shard.from_env(env)
        if self.shard.count > 1 and not shard_isolation.strip():
            raise ValueError("multiple shards require verified independent fixture/reset evidence")
        self.shard_isolation = shard_isolation
        self.tags = tuple(target_tags)
        self.clock = clock
        self.alarm = alarm
        self.collected: list[str] = []
        self.selected: list[str] = []
        self.nodes: list[dict] = []
        self.errors: list[str] = []
        self.collection_errors: list[dict] = []
        self.current: dict | None = None
        self.deadline: Deadline | None = None
        self.expired = False
        self.phase_durations: dict[str, float] = {}
        self.collection_finished = False

    def pytest_configure(self, config):
        if getattr(config.option, "numprocesses", 0) or hasattr(config, "workerinput"):
            raise pytest.UsageError("Bazel owns parallelism; pytest must run serially")
        for plugin in ("timeout", "rerunfailures", "flaky", "repeat"):
            if config.pluginmanager.hasplugin(plugin):
                raise pytest.UsageError(f"disable {plugin}: Bazel runtime owns budgets/no retries")
        config.addinivalue_line("markers", "flaky(owner, evidence): visible no-cache quarantine")
        self.shard.acknowledge()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        # Resume after marker/-k selection and other collection modifiers.
        yield
        self.collected = [normalize_nodeid(item.nodeid) for item in items]
        try:
            self.selected = self.shard.select(self.collected)
            for item in items:
                marker = item.get_closest_marker("flaky")
                if marker:
                    if marker.args or set(marker.kwargs) != {"owner", "evidence"}:
                        raise ValueError("flaky requires exactly owner= and evidence= keywords")
                    flake_contract(tags=self.tags, **marker.kwargs)
        except (ValueError, TypeError) as exc:
            raise pytest.UsageError(str(exc)) from exc
        chosen = set(self.selected)
        deselected = [item for item in items if normalize_nodeid(item.nodeid) not in chosen]
        items[:] = [item for item in items if normalize_nodeid(item.nodeid) in chosen]
        config.hook.pytest_deselected(items=deselected)

    def pytest_collection_finish(self, session):
        self.collection_finished = True

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_errors.append(
                {"nodeid": report.nodeid, "outcome": report.outcome, "reason": str(report.longrepr)}
            )

    def pytest_internalerror(self, excrepr, excinfo):
        self.errors.append(str(excrepr))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        nodeid = normalize_nodeid(item.nodeid)
        marker = item.get_closest_marker("flaky")
        self.current = {
            "nodeid": nodeid,
            "parameter_id": getattr(getattr(item, "callspec", None), "id", None),
            "phases": {},
            "shared_setup": [],
            "flake": flake_contract(tags=self.tags, **marker.kwargs) if marker else None,
        }
        self.nodes.append(self.current)
        self.deadline = Deadline(TEST_SECONDS, self.clock)
        self.phase_durations = {}
        self.expired = False
        try:
            return (yield)
        finally:
            self.current["elapsed"] = self.deadline.elapsed
            phases = self.current["phases"]
            self.current["outcome"] = node_outcome(phases)
            self.current["budget_exceeded"] = self.expired or self.deadline.elapsed > TEST_SECONDS
            if self.current["budget_exceeded"]:
                self.errors.append(f"test exceeded 60s: {nodeid}")
            self.current = None
            self.deadline = None

    @contextmanager
    def _phase(self, name):
        begin = self.clock()
        try:
            # Once expired, finalizers still run under the launcher's target bound.
            if self.expired:
                yield
                return
            try:
                self.deadline.check("test including fixture phases")
                with self.alarm(self.deadline):
                    yield
                self.deadline.check("test including fixture phases")
            except BudgetExpired:
                self.expired = True
                raise
        finally:
            self.phase_durations[name] = self.clock() - begin

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        with self._phase("setup"):
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        with self._phase("call"):
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        with self._phase("teardown"):
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        begin = self.clock()
        try:
            return (yield)
        finally:
            if self.current is not None and fixturedef.scope != "function":
                self.current["shared_setup"].append(
                    {
                        "fixture": fixturedef.argname,
                        "scope": fixturedef.scope,
                        "duration": self.clock() - begin,
                    }
                )

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if self.current is None:
            self.errors.append(f"phase without protocol: {item.nodeid}")
            return report
        # An xfail marker must never turn a budget failure green.
        if self.expired or self.deadline.elapsed > TEST_SECONDS:
            self.expired = True
            report.outcome = "failed"
            report.longrepr = f"Bazel runtime: {item.nodeid} exceeded 60s including fixture phases"
            if hasattr(report, "wasxfail"):
                del report.wasxfail
        if report.when in self.current["phases"]:
            self.errors.append(f"duplicate phase/retry: {item.nodeid} {report.when}")
        identity_properties(item, report)
        strict = json.loads(dict(report.user_properties)["aq_xfail_strict"])
        self.current["phases"][report.when] = {
            **phase_result(report, strict),
            "duration": self.phase_durations[report.when],
        }
        return report

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if not self.collection_finished or not self.collected:
            self.errors.append("missing or empty suite collection")
        if session.config.option.collectonly:
            self.errors.append("collection-only does not verify execution")
        if Counter(n["nodeid"] for n in self.nodes) != Counter(self.selected):
            self.errors.append("selected nodes did not execute exactly once")
        for node in self.nodes:
            phases = node["phases"]
            if "setup" not in phases or "teardown" not in phases:
                self.errors.append(f"incomplete phases: {node['nodeid']}")
            elif phases["setup"]["outcome"] == "passed" and "call" not in phases:
                self.errors.append(f"missing call: {node['nodeid']}")
        if self.errors or self.collection_errors:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        elif exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not self.selected:
            # A legitimately empty partition still emits a required shard receipt.
            session.exitstatus = pytest.ExitCode.OK
        payload = {
            "schema_version": SCHEMA_VERSION,
            "shard": {"index": self.shard.index, "count": self.shard.count},
            "shard_isolation": self.shard_isolation,
            "collected": self.collected,
            "selected": self.selected,
            "nodes": self.nodes,
            "errors": self.errors,
            "collection_errors": self.collection_errors,
            "exit_status": int(session.exitstatus),
        }
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.output)
