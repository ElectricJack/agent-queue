"""Inventory boundary and delivery-accounting regressions on synthetic evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/bazel/{name}.py")
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


inventory = module("inventory")
report = module("report_metrics")


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure evidence accounting; no database resources."""


def at(minutes):
    return f"2026-10-23T{minutes // 60:02}:{minutes % 60:02}:00Z"


def key(**changes):
    return {"task_id": "task", "head_sha": "head", "base_sha": "base",
            "candidate_revision": "candidate-1", "commit_sha": "exact-candidate-sha", "workflow_id": "tests",
            "run_id": "run-1", "run_attempt": 1, "suite_version": "v1", **changes}


def suite(name="python", **changes):
    return {**key(), "suite": name, "status": "completed", "conclusion": "success",
            "queued_at": at(1), "started_at": at(3), "completed_at": at(10),
            "billed_minutes": 7, **changes}


def document():
    return {"schema_version": 1, "as_of": at(600),
            "attempts": [{**key(), "first_push_at": at(0),
                          "required_suites": ["python", "dashboard"]}],
            "suites": [suite(), suite("dashboard", completed_at=at(20))],
            "deliveries": [{"task_id": "task", "head_sha": "head", "base_sha": "base",
                            "first_commit_at": at(0), "landed_at": at(30),
                            "rebase_events": 2, "rerun_events": 1}]}


def test_complete_metrics_have_explicit_denominators_and_nearest_rank_percentiles():
    result = report.metrics(document())
    assert result["metrics"]["push_to_green_seconds"] == {
        "population": 1, "observed": 1, "missing": 0, "p50": 1200, "p90": 1200, "sum": 1200}
    assert result["metrics"]["first_commit_to_dev_seconds"]["p50"] == 1800
    assert result["metrics"]["billed_minutes_per_landing"]["sum"] == 14
    assert result["attempts"][0]["queue_seconds"]["sum"] == 240
    assert report.distribution([1, 2, None, 3, 4, 5, 6, 7, 8, 9, 10])["p90"] == 9
    assert result["missing_sources"] == ["worker_waits", "events", "incidents", "prs"]


@pytest.mark.parametrize("field", report.JOIN_FIELDS)
def test_a_green_suite_from_another_exact_identity_cannot_satisfy_an_attempt(field):
    data = document()
    data["suites"][1][field] = "different"
    result = report.metrics(data)
    assert result["metrics"]["push_to_green_seconds"]["missing"] == 1
    assert result["attempts"][0]["all_required_green"] is False
    assert len(result["unjoined_suite_observations"]) == 1


@pytest.mark.parametrize("status,conclusion", [
    ("completed", "failure"), ("completed", "cancelled"), ("queued", None),
    ("in_progress", None), ("completed", "skipped"), ("completed", None),
])
def test_unsuccessful_observations_remain_in_the_population(status, conclusion):
    data = document()
    data["suites"][0].update(status=status, conclusion=conclusion)
    result = report.metrics(data)
    assert result["metrics"]["push_to_green_seconds"]["population"] == 1
    assert result["metrics"]["push_to_green_seconds"]["observed"] == 0
    assert result["suite_observations"][0]["status"] == status


def test_missing_duplicate_incomplete_and_future_observations_never_certify_green():
    for mutation in ("missing", "duplicate", "identity", "future", "duplicate_attempt"):
        data = document()
        if mutation == "missing":
            data["suites"].pop()
        elif mutation == "duplicate":
            data["suites"].append(copy.deepcopy(data["suites"][1]))
        elif mutation == "identity":
            del data["attempts"][0]["base_sha"]
        elif mutation == "future":
            data["suites"][1]["completed_at"] = at(700)
        else:
            data["attempts"].append(copy.deepcopy(data["attempts"][0]))
        result = report.metrics(data)
        assert result["metrics"]["push_to_green_seconds"]["observed"] == 0
        assert all(a["issues"] for a in result["attempts"])


def test_wait_time_unions_overlaps_per_worker_and_clips_to_window():
    result = report.interval_union([
        {"worker_id": "a", "start": at(0), "end": at(10), "reason": "checks"},
        {"worker_id": "a", "start": at(5), "end": at(15), "reason": "checks"},
        {"worker_id": "b", "start": at(5), "end": at(10), "reason": "checks"},
        {"worker_id": "a", "start": at(1), "end": None, "reason": "checks"},
        {"worker_id": "a", "start": at(0), "end": at(30), "reason": "coding"},
    ], report.timestamp(at(5)), report.timestamp(at(12)))
    assert result["seconds"] == (7 + 5) * 60
    assert result["missing_intervals"] == 1


def test_event_incident_dedup_and_green_mergeable_wait_are_explicit():
    data = document()
    data.update(events=[{"id": "start", "kind": "train_start"}] * 2 + [{"kind": "train_abort"}],
                incidents=[{"id": "a", "root_cause": "same"},
                           {"id": "b", "root_cause": "same"}, {"id": "c"}],
                prs=[{"id": 1, "green": True, "mergeable": True, "waiting_since": at(0)},
                     {"id": 2, "green": True, "mergeable": False, "waiting_since": at(0)}])
    metrics = report.metrics(data)["metrics"]
    assert metrics["train_events"] == {"train_start": 1}
    assert metrics["missing_event_ids"] == 1
    assert metrics["red_dev_root_causes"] == 1
    assert metrics["incidents_missing_root_cause"] == 1
    assert [r["id"] for r in metrics["green_mergeable_prs_waiting_over_2h"]] == [1]


def test_window_exclusions_and_missing_billing_are_visible_and_input_is_not_mutated():
    data = document()
    data.update(window_start=at(0), window_end=at(15))
    data["suites"][0].pop("billed_minutes")
    original = copy.deepcopy(data)
    result = report.metrics(data)
    assert data == original
    assert result["excluded_or_undated_deliveries"] == original["deliveries"]
    data.pop("window_start")
    data.pop("window_end")
    assert report.metrics(data)["metrics"]["billed_minutes_per_landing"]["missing"] == 1


def test_history_unknowns_need_attempt_bound_evidence_and_baseline_is_recorded(tmp_path):
    baseline = tmp_path / "baseline.md"
    baseline.write_text('- `tests/test_prior.py::test_failure`\n', encoding="utf-8")
    runs = [{"id": i, "run_attempt": 1, "head_sha": str(i), "created_at": at(i),
             "status": "completed", "conclusion": "failure", "token": "secret"} for i in range(4)]
    runs.append({"id": 9, "run_attempt": 1, "status": "queued"})
    claims = [{"run_id": 0, "run_attempt": 1, "classification": "test_flake", "evidence": []},
              {"run_id": 1, "run_attempt": 1, "classification": "test_flake", "evidence": ["log:123"]},
              {"run_id": 2, "run_attempt": 2, "classification": "infrastructure", "evidence": ["log:99"]}]
    result = report.history([{"workflow_runs": runs}] * 2, claims, baseline)
    assert result["completed_count"] == 4
    assert result["shortage"] == 296
    assert result["failure_classes"] == {"unknown": 3, "test_flake": 1}
    assert len(result["active_runs"]) == 1
    assert result["baseline"]["known_failed_nodeids"] == ["tests/test_prior.py::test_failure"]
    assert "secret" not in json.dumps(result)
    assert all(r["run_seconds"] is None for r in result["completed_runs"])


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "3"])
def test_invalid_numeric_evidence_is_refused(value):
    with pytest.raises(ValueError):
        report.distribution([value])


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError, match="timezone"):
        report.timestamp("2026-10-23T00:00:00")


def test_missing_and_duplicate_required_bills_are_unknown_per_landing():
    data = document()
    data["suites"].pop()
    assert report.metrics(data)["metrics"]["billed_minutes_per_landing"]["missing"] == 1
    data = document()
    data["suites"].append(copy.deepcopy(data["suites"][0]))
    assert report.metrics(data)["metrics"]["billed_minutes_per_landing"]["missing"] == 1


def test_future_delivery_and_wait_evidence_cannot_inflate_current_metrics():
    data = document()
    data["deliveries"][0]["landed_at"] = at(700)
    data["worker_waits"] = [{"worker_id": "a", "reason": "checks", "start": at(590), "end": at(700)}]
    result = report.metrics(data)
    assert result["metrics"]["first_commit_to_dev_seconds"]["missing"] == 1
    assert result["metrics"]["worker_check_wait"]["seconds"] == 600


def test_resolved_fixtures_and_shadowed_database_fixture_are_collected(pytester):
    pytester.makeconftest('''
        import pytest
        @pytest.fixture
        def db():
            return "not a database"
        @pytest.fixture
        def consumer(db):
            return db
    ''')
    pytester.makepyfile('''
        import pytest
        @pytest.mark.parametrize("param", [1, 2], ids=["one", "two"])
        def test_consumer(consumer, param):
            raise AssertionError("collection must not execute")
    ''')
    collector = inventory.Collector(pytester.path)
    result = pytester.runpytest_inprocess("--collect-only", "-q", plugins=[collector])
    assert result.ret == 0
    data = collector.document
    assert len(data["nodes"]) == 2
    assert data["nodes"][0]["parameter_id"] == "one"
    assert "db" in data["nodes"][0]["fixtures"]
    assert data["nodes"][0]["resources"]["postgres"]["status"] == "unknown"
    db = next(f for f in data["fixtures"] if f["name"] == "consumer")
    assert db["dependencies"] == ["db"]


def test_fixture_override_and_super_fixture_determine_actual_pg_evidence(pytester):
    pytester.makeconftest('''
        import pytest
        class LeasePool: pass
        @pytest.fixture(autouse=True)
        def database():
            return LeasePool()
    ''')
    pytester.makepyfile(test_override='''
        import pytest
        @pytest.fixture(autouse=True)
        def database(): pass
        def test_pure(): pass
    ''', test_super='''
        import pytest
        @pytest.fixture(autouse=True)
        def database(database): return database
        def test_pg(): pass
    ''')
    collector = inventory.Collector(pytester.path)
    assert pytester.runpytest_inprocess("--collect-only", "-q", plugins=[collector]).ret == 0
    nodes = {n["module"]: n for n in collector.document["nodes"]}
    assert nodes["test_override.py"]["resources"]["postgres"]["status"] == "unknown"
    assert nodes["test_super.py"]["resources"]["postgres"]["status"] == "evidence"


def test_workflow_marker_arms_and_actual_dashboard_obligations_are_inventoried():
    jobs = inventory.workflow_inventory(ROOT)
    test_jobs = [j for j in jobs if j["workflow"] == ".github/workflows/tests.yml"]
    assert len([j for j in test_jobs if j["job"] == "test"]) == 11
    groups = [j["matrix"]["group"] for j in test_jobs if j["job"] == "e2e-cli"]
    assert sorted(groups) == ["claims", "cli", "failover", "graphs"]
    dashboard = next(j for j in test_jobs if j["job"] == "dashboard")
    commands = [s["command"] for s in dashboard["steps"]]
    assert "npm run typecheck" in commands and "npm run build" in commands
    assert "npm run test" not in commands
    assert any("test_cli_startup_offline.py" in s["command"]
               for j in test_jobs for s in j["steps"])
    data = inventory.build_inventory(ROOT, {"nodes": [], "fixtures": []}, {}, [], [])
    assert data["counts"]["python_obligations"] == 16


def test_marker_and_keyword_membership_preserve_exclusions_and_duplicates():
    nodes = [{"nodeid": "tests/test_x.py::test_one[claims-S1]", "module": "tests/test_x.py",
              "markers": ["integration"], "keywords": ["test_one[claims-S1]"]},
             {"nodeid": "tests/test_x.py::test_two", "module": "tests/test_x.py",
              "markers": ["slow", "integration"], "keywords": ["test_two"]}]
    assert inventory.select_nodes(nodes, "pytest tests/ -m 'integration and not slow'", "") == [nodes[0]["nodeid"]]
    assert inventory.select_nodes(nodes, "pytest tests/ -k 'claims-' -m integration", "") == [nodes[0]["nodeid"]]
    assert inventory.select_nodes(nodes, "pytest tests/ --ignore=tests/test_x.py", "") == []
    for i, node in enumerate(nodes):
        node.update(collection_index=i, split_sort_name=node["nodeid"])
    # Each arm is a disjoint partition of the same obligations, not eight copies.
    groups = [inventory.partition(nodes, [n["nodeid"] for n in nodes],
                                  ["--splits", "2", "--group", str(group),
                                   "--splitting-algorithm", "least_duration"],
                                  {nodes[0]["nodeid"]: 2, nodes[1]["nodeid"]: 1})[0]
              for group in (1, 2)]
    assert set(groups[0]).isdisjoint(groups[1])
    assert set(groups[0] + groups[1]) == {n["nodeid"] for n in nodes}


def test_historical_total_is_not_manufactured_into_phase_observations(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts=""\n')
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard/package.json").write_text('{"scripts": {"test": "vitest run"}}')
    collection = {"nodes": [{"nodeid": "a", "module": "a.py", "markers": []}], "fixtures": []}
    result = inventory.build_inventory(tmp_path, collection, {"a": 70, "stale": 8}, [], [])
    assert result["timings"][0]["phase_observations"] == []
    assert result["stale_timing_nodeids"] == ["stale"]
    assert result["counts"]["nodes_missing_phase_timings"] == 1
    assert result["counts"]["historical_nodes_over_60_seconds"] == 1


pytest_plugins = ["pytester"]
