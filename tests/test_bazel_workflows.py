"""Shadow workflow trust boundaries and fail-closed CI evidence accounting."""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from src.integration import suite_cutover as cutover

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/bazel"))
_spec = importlib.util.spec_from_file_location("bazel_ci_sample", ROOT / "scripts/bazel/ci_sample.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)
evidence = ci.evidence


@pytest.fixture(autouse=True)
def _pg_backend():
    """Artifact accounting does not allocate databases."""


def events(*, cached=False, count=3):
    rows = []
    for i in range(count):
        label = f"//tests:target_{i}"
        rows.extend([
            {"id": {"testResult": {"label": label, "run": 1, "shard": 1, "attempt": 1}},
             "testResult": {"status": "PASSED", "cachedLocally": cached,
                            "testAttemptDuration": f"{i + 1}.500s"}},
            {"id": {"testSummary": {"label": label}},
             "testSummary": {"overallStatus": "PASSED", "shardCount": 1, "totalRunCount": 1}},
        ])
    rows += [{"id": {"buildMetrics": {}}, "buildMetrics": {
        "actionSummary": {"actionsExecuted": "0" if cached else "4", "actionsCreated": "999"},
        "buildGraphMetrics": {"actionCount": "10"}}},
        {"id": {"buildFinished": {}}, "finished": {"exitCode": {"name": "SUCCESS"}},
         "lastMessage": True}]
    return [{"id": {"started": {}}, "started": {},
             "children": [row["id"] for row in rows]}] + rows


def target_names():
    return [f"//tests:target_{i}" for i in range(3)]


def test_cold_and_cached_bep_have_real_denominators_and_five_slowest():
    cold = evidence.bep_report(events(), target_names())
    assert cold["complete"] and not cold["all_tests_cached"]
    assert cold["executed_target_fraction"] == 1
    assert cold["executed_action_fraction"] == 0.4  # not actionsCreated=999
    assert [r["seconds"] for r in cold["five_slowest"]] == [3.5, 2.5, 1.5]
    warm = evidence.bep_report(events(cached=True), target_names())
    assert warm["all_tests_cached"]
    assert warm["executed_targets"] == warm["executed_actions"] == 0
    assert warm["cache_sources"] == {"local": 3}


@pytest.mark.parametrize("fault", ["truncated", "no_finish", "no_exit", "cancelled", "failed",
                                   "missing_target", "missing_shard", "duplicate", "retry",
                                   "no_actions", "no_metrics", "no_last"])
def test_bep_cancellation_incompleteness_and_retry_are_never_success(fault):
    rows = events(cached=True)
    if fault == "truncated":
        rows.pop(1)
    elif fault == "no_finish":
        rows.pop()
    elif fault == "no_exit":
        rows[-1]["finished"].pop("exitCode")
    elif fault == "cancelled":
        rows[1]["aborted"] = {"reason": "USER_INTERRUPTED"}
    elif fault == "failed":
        rows[1]["testResult"]["status"] = "FAILED"
    elif fault == "missing_target":
        rows = rows[:1] + rows[3:]
    elif fault == "missing_shard":
        rows[2]["testSummary"]["shardCount"] = 2
    elif fault == "duplicate":
        rows.append(copy.deepcopy(rows[1]))
    elif fault == "retry":
        rows[1]["id"]["testResult"]["attempt"] = 2
    elif fault == "no_actions":
        rows[-2]["buildMetrics"].pop("buildGraphMetrics")
    elif fault == "no_metrics":
        rows.pop(-2)
    else:
        rows[-1].pop("lastMessage")
    result = evidence.bep_report(rows, target_names())
    assert not result["complete"] and not result["all_tests_cached"]
    assert result["issues"]


def test_cached_analysis_reuses_only_explicit_same_graph_action_denominator():
    rows = events(cached=True)
    rows[-2]["buildMetrics"].pop("buildGraphMetrics")
    result = evidence.bep_report(rows, target_names(), prior_eligible_actions=10)
    assert result["complete"] and result["executed_action_fraction"] == 0
    assert "same-graph cold" in result["eligible_action_source"]


def test_disk_hits_are_distinguished_from_server_result_hits():
    rows = events()
    for row in rows:
        if "testResult" in row:
            row["testResult"]["executionInfo"] = {"cachedRemotely": True}
    result = evidence.bep_report(rows, target_names())
    assert result["all_tests_cached"] and result["cache_sources"] == {"disk": 3}


def test_bazel_unsharded_summary_omits_zero_shards_and_ids_are_one_based():
    rows = events(cached=True)
    for row in rows:
        if "testSummary" in row:
            row["testSummary"].pop("shardCount")
    assert evidence.bep_report(rows, target_names())["complete"]
    rows[1]["id"]["testResult"]["shard"] = 0
    assert not evidence.bep_report(rows, target_names())["complete"]


def sample(sha, **changes):
    return {"schema_version": 1, "sha": sha, "event": "push", "ref": "refs/heads/dev",
            "trusted": True, "suites": {"python": {"success": True},
                                        "dashboard": {"success": True}}, **changes}


def test_twenty_consecutive_commits_are_per_suite_and_missing_samples_reset():
    commits = [str(i) for i in range(23)]
    samples = [sample(sha) for sha in commits]
    samples[2]["suites"]["python"]["success"] = False
    report = evidence.window_report(commits, samples, ["python", "dashboard"])
    assert report["counters"] == {"python": 20, "dashboard": 23}
    assert report["observational"] is True
    samples.pop(21)
    report = evidence.window_report(commits, samples, ["python", "dashboard"])
    assert report["counters"] == {"python": 1, "dashboard": 1}
    assert report["commits"][21]["missing_or_ambiguous"]


@pytest.mark.parametrize("change", [{"event": "pull_request"}, {"trusted": False},
                                    {"ref": "refs/heads/main"}, {"schema_version": 99},
                                    {"suites": {}}, {"suites": {"python": {"success": 1}}}])
def test_window_rejects_other_event_trust_schema_and_missing_evidence(change):
    result = evidence.window_report(["a", "b"], [sample("a"), sample("b", **change)], ["python"])
    assert result["counters"]["python"] == 0


def test_duplicate_attempts_cannot_cherry_pick_green_and_duplicate_history_is_refused():
    result = evidence.window_report(["a"], [sample("a"), sample("a")], ["python"])
    assert result["counters"]["python"] == 0
    with pytest.raises(ValueError, match="duplicate dev commits"):
        evidence.window_report(["a", "a"], [], ["python"])


def test_retained_bep_outputs_preserve_original_bytes_and_refuse_remote_urls(tmp_path):
    original = tmp_path / "original.json"
    original.write_bytes(b'{"cached":"bytes"}\n')
    rows = [{"id": {"testResult": {"label": "//tests:x"}}, "testResult": {
        "testActionOutput": [{"name": "test.outputs/pytest.json", "uri": original.as_uri()}]}}]
    retained = evidence.retain_test_outputs(rows, tmp_path / "evidence")
    assert evidence.digest(retained["//tests:x"][0]["files"]["pytest.json"]) == evidence.digest(original)
    rows[0]["testResult"]["testActionOutput"][0]["uri"] = "https://example.invalid/token"
    with pytest.raises(ValueError, match="local file"):
        evidence.retain_test_outputs(rows, tmp_path / "unsafe")


async def test_wrong_sha_fails_before_execution(monkeypatch):
    async def head(*args):
        return "a" * 40
    monkeypatch.setattr(ci, "git", head)
    with pytest.raises(ValueError, match="exact sample SHA"):
        await ci.context("0" * 40)


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_trigger_sha_runner_and_cache_trust_matrix():
    shadow, legacy = workflow("bazel-shadow.yml"), workflow("bazel-legacy-sample.yml")
    triggers = shadow.get("on", shadow.get(True))
    assert triggers["push"]["branches"] == ["dev"]
    assert "pull_request" in triggers and "pull_request_target" not in triggers
    assert "concurrency" not in shadow  # never cancel consecutive dev samples
    assert shadow["jobs"]["legacy"]["with"]["sha"] == "${{ github.sha }}"
    for document in (shadow, legacy):
        assert document["permissions"]["contents"] == "read"
        for job in document["jobs"].values():
            if "runs-on" in job:
                assert job["runs-on"] == "ubuntu-latest"
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["ref"] in ("${{ github.sha }}", "${{ inputs.sha }}")
                    assert step["with"]["persist-credentials"] is False
                if step.get("uses", "").startswith("actions/upload-artifact@"):
                    assert step["if"] == "always()"
                    assert step["with"]["retention-days"] >= 90
                    assert step["with"]["if-no-files-found"] == "error"
    steps = shadow["jobs"]["bazel"]["steps"]
    namespace = next(s for s in steps if s.get("id") == "namespace")
    assert "github.ref_protected" in namespace["env"]["TRUSTED"]
    assert "protected-dev" in namespace["run"] and "untrusted-" in namespace["run"]
    saves = [s for s in steps if s.get("uses", "").startswith("actions/cache/save@")]
    assert len(saves) == 2
    assert "github.ref_protected" in saves[0]["if"] and "'push'" in saves[0]["if"]
    assert "'pull_request'" in saves[1]["if"]
    assert all("github.sha" in s["with"]["key"] for s in saves)


def test_legacy_matrix_preserves_all_python_arms_and_dashboard_commands(tmp_path):
    plan = ci.plan_document()
    names = ci.suite_names(plan)
    assert len(names) == 18  # eleven Python arms, startup, four E2E, two dashboard
    assert all(f"default_{i}" in names for i in range(1, 9))
    for name in names:
        if name in ci.DASHBOARD:
            continue
        args = ci.legacy_arguments(name, plan, tmp_path)
        assert "--aq-outcomes" in args and "tools.bazel.pytest_plugin" in args
        assert sum(a.startswith("--junitxml=") for a in args) == 1
        assert plan["suites"][name]["marker"]


def test_existing_parity_comparator_receives_complete_projected_evidence(tmp_path):
    from tests.test_bazel_parity import evidence as fixture_evidence

    left, right = tmp_path / "legacy", tmp_path / "bazel"
    fixture_evidence(left, dashboard=True)
    fixture_evidence(right, dashboard=True)
    result = ci.compare_suite(left, right, "default_1", tmp_path / "comparison")
    assert result["success"]
    assert evidence.read(left / "manifest.json")["required_suites"] == ["default_1", "dashboard_build"]
    (right / "outcomes-0.json").unlink()
    with pytest.raises(OSError):
        ci.compare_suite(left, right, "default_1", tmp_path / "missing")


async def test_missing_backend_artifacts_still_publish_a_failed_sample(tmp_path, monkeypatch):
    plan = {"suites": {"python": {"command": "pytest", "marker": ""}}}
    monkeypatch.setattr(ci, "plan_document", lambda: plan)
    monkeypatch.setattr(ci, "DASHBOARD", ())
    async def current_context(sha):
        return {"sha": sha}
    async def no_parent(sha):
        return {"category": "unknown"}
    monkeypatch.setattr(ci, "context", current_context)
    monkeypatch.setattr(ci, "change_report", no_parent)
    inputs, output = tmp_path / "inputs", tmp_path / "report"
    assert await ci.sample(output, "a" * 40, inputs) == 1
    result = evidence.read(output / "sample.json")
    assert result["success"] is False and result["suites"]["python"]["issues"]


@pytest.mark.parametrize("fault", [None, "old_sha", "executed_warm", "changed_cache_bytes",
                                   "legacy_failed"])
async def test_complete_sample_requires_current_candidate_and_unchanged_receipts(
    tmp_path, monkeypatch, fault
):
    from tests.test_bazel_parity import evidence as fixture_evidence

    inputs, output = tmp_path / "inputs", tmp_path / "report"
    manifest = fixture_evidence(inputs / "legacy-default_1")
    manifest["invocation"]["exit_status"] = int(fault == "legacy_failed")
    evidence.write(inputs / "legacy-default_1/manifest.json", manifest)
    for phase in evidence.PHASES:
        fixture_evidence(inputs / "bazel" / phase)
    executions = {phase: {"complete": True, "exit_status": 0, "all_tests_cached": True}
                  for phase in evidence.PHASES}
    if fault == "executed_warm":
        executions["warm"]["all_tests_cached"] = False
    if fault == "changed_cache_bytes":
        root = inputs / "bazel/warm"
        path = root / "outcomes-0.json"
        path.write_bytes(path.read_bytes() + b"\n")
        value = evidence.read(root / "manifest.json")
        value["suites"]["default_1"]["targets"][0]["shards"][0]["sidecar"] = (
            evidence.reference(root, path))
        evidence.write(root / "manifest.json", value)
    evidence.write(inputs / "bazel/executions.json", executions)
    monkeypatch.setattr(ci, "plan_document", lambda: {
        "suites": {"default_1": {"command": "pytest"}}})
    monkeypatch.setattr(ci, "DASHBOARD", ())

    async def current_context(sha):
        return {**manifest["context"], "sha": sha}
    async def no_parent(sha):
        return {"category": "unknown"}
    monkeypatch.setattr(ci, "context", current_context)
    monkeypatch.setattr(ci, "change_report", no_parent)
    sha = "c" * 40 if fault == "old_sha" else "a" * 40
    assert await ci.sample(output, sha, inputs) == int(fault is not None)
    assert evidence.read(output / "sample.json")["success"] is (fault is None)
    assert "observational" in (output / "summary.md").read_text()


def test_cache_measurement_keeps_unavailable_provider_values_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("CACHE_PREFIX", "trusted-")
    for name, value in (("restore-start", 1000000000), ("restore-end", 3000000000),
                        ("save-start", 4000000000), ("save-end", 7000000000)):
        (tmp_path / ("cache-" + name)).write_text(str(value))
    def api(path):
        raise OSError("permission unavailable")
    monkeypatch.setattr(ci, "github_api", api)
    result = ci.cache_report(tmp_path / "cache")
    assert result["restore_seconds"] == 2 and result["save_seconds"] == 3
    assert result["provider_limit_bytes"] is result["eviction_rate"] is None
    assert result["inventory_error"] == "permission unavailable"


def test_cache_measurement_reads_current_provider_limit_without_fixed_quota(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("CACHE_PREFIX", "protected-")
    def api(path):
        value = ({"max_cache_size_gb": 23} if path.endswith("storage-limit") else
                 {"active_caches_size_in_bytes": 4_600_000_000} if path.endswith("usage") else
                 {"actions_caches": [], "total_count": 0})
        return json.dumps(value).encode()
    monkeypatch.setattr(ci, "github_api", api)
    result = ci.cache_report(tmp_path)
    assert result["provider_limit_bytes"] == 23_000_000_000
    assert result["provider_usage_fraction"] == 0.2


def test_artifact_redirect_does_not_forward_github_token():
    request = ci.urllib.request.Request("https://api.github.com/repos/example/repo/zip",
                                        headers={"Authorization": "Bearer example"})
    redirected = ci.TokenScopedRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://artifacts.example.invalid/signed")
    assert not redirected.has_header("Authorization")
    with pytest.raises(ValueError, match="HTTPS"):
        ci.TokenScopedRedirect().redirect_request(request, None, 302, "Found", {},
                                                 "http://artifacts.example.invalid/signed")


def test_cutover_workflows_use_exact_head_and_catalog_while_legacy_fallback_stays():
    tests, candidate, audit = (workflow(name) for name in
                               ("tests.yml", "train-candidate.yml", "main-attestation.yml"))
    jobs = tests["jobs"]
    assert all(name in jobs for name in ("test", "e2e-cli", "dashboard"))
    assert jobs["suite-policy"]["steps"][-1]["run"] == (
        "python3 src/integration/suite_cutover.py workflow")
    bazel = jobs["bazel-suite"]
    assert bazel["name"] == "${{ matrix.suite.name }}"
    assert bazel["if"] == "needs.suite-policy.outputs.enabled == 'true'"
    assert bazel["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    command = next(step for step in bazel["steps"] if step.get("env", {}).get("BAZEL_TARGET"))
    assert command["env"]["BAZEL_TARGET"] == "${{ matrix.suite.target }}"
    assert '"$BAZEL_TARGET"' in command["run"] and "--config=ci" in command["run"]
    mark = candidate["jobs"]["mark"]
    triggers = candidate.get("on", candidate.get(True))
    assert triggers["workflow_run"]["types"] == ["requested", "in_progress", "completed"]
    invalidation = candidate["jobs"]["invalidate"]["steps"][0]
    assert "'failure' || 'pending'" in invalidation["env"]["STATE"]
    assert '-f state="$STATE"' in invalidation["run"]
    assert mark["needs"] == "invalidate"
    assert "workflow_run.conclusion == 'success'" in mark["if"]
    assert "github.event.action == 'completed'" in mark["if"]
    assert mark["steps"][0]["with"]["path"] == "trusted"
    assert mark["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    assert mark["steps"][1]["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert "trusted/src/integration/suite_cutover.py candidate" in mark["steps"][2]["run"]
    assert mark["steps"][-1]["if"] == "failure()"
    assert "-f state=failure" in mark["steps"][-1]["run"]
    assert audit["jobs"]["unattested-ci"]["uses"] == "./.github/workflows/tests.yml"
    assert audit["jobs"]["attestation"]["steps"][-1]["env"]["TRUST_MANIFEST_PATH"] == (
        cutover.TRUST_PATH)
