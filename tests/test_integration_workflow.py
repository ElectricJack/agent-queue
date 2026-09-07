from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-integration-attestation.py"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SHA = "a" * 40
CANDIDATE_REF = "aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32


def _run_record(
    record_id: object = 41,
    *,
    attempt: object = 2,
    branch: str = CANDIDATE_REF,
    conclusion: str | None = "success",
    event: str = "push",
    head_sha: str = SHA,
    repository: str = "acme/widgets",
    head_repository: str = "acme/widgets",
    workflow_id: object = 17,
    path: str = ".github/workflows/tests.yml@" + CANDIDATE_REF,
) -> dict:
    return {
        "id": record_id,
        "run_attempt": attempt,
        "workflow_id": workflow_id,
        "name": "Tests",
        "path": path,
        "event": event,
        "head_branch": branch,
        "head_sha": head_sha,
        "status": "completed" if conclusion is not None else "in_progress",
        "conclusion": conclusion,
        "repository": {"full_name": repository},
        "head_repository": {"full_name": head_repository},
    }


def _current_run() -> dict:
    return {
        **_run_record(
            52,
            attempt=1,
            branch="main",
            conclusion=None,
            path=".github/workflows/tests.yml@main",
        ),
        "event": "push",
    }


def _evidence(*runs: dict) -> dict:
    if not runs:
        runs = (_run_record(),)
    return {
        "current_run": _current_run(),
        "workflow_runs": [
            {"listed": run, "attempt": dict(run), "latest": dict(run)} for run in runs
        ],
    }


def _invoke(tmp_path: Path, evidence: object, **changes: str) -> subprocess.CompletedProcess[str]:
    evidence_path = tmp_path / "workflow-runs.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    args = {
        "event-name": "push",
        "ref": "refs/heads/main",
        "default-branch": "main",
        "repository-full-name": "acme/widgets",
        "checkout-sha": SHA,
        "current-run-id": "52",
        "current-run-attempt": "1",
    }
    args.update(changes)
    command = [sys.executable, str(SCRIPT)]
    for key, value in args.items():
        command.extend((f"--{key}", value))
    command.extend(("--workflow-runs-file", str(evidence_path)))
    return subprocess.run(command, check=True, text=True, capture_output=True)


def _run(tmp_path: Path, evidence: object | None = None, **changes: str) -> str:
    result = _invoke(tmp_path, _evidence() if evidence is None else evidence, **changes)
    assert result.stderr == ""
    return result.stdout.strip()


def _matching_pr(*, head_repository: str, head_ref: str) -> str:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--matching-integration-pr",
            "--event-name",
            "pull_request",
            "--repository-full-name",
            "acme/widgets",
            "--head-repository-full-name",
            head_repository,
            "--head-ref",
            head_ref,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_workflow_decision_reuses_exact_successful_candidate_run(tmp_path):
    assert _run(tmp_path) == "true"


@pytest.mark.parametrize(
    "changes",
    [
        {"event-name": "pull_request"},
        {"ref": "refs/heads/aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32},
        {"default-branch": "trunk"},
        {"repository-full-name": "acme/other"},
        {"checkout-sha": "b" * 40},
        {"current-run-id": "51"},
        {"current-run-attempt": "2"},
    ],
)
def test_workflow_decision_only_skips_for_exact_default_branch_push(tmp_path, changes):
    assert _run(tmp_path, **changes) == "false"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("head_branch", "aq/integration/not-generated"),
        ("event", "pull_request"),
        ("repository", {"full_name": "fork/widgets"}),
        ("head_repository", {"full_name": "fork/widgets"}),
        ("workflow_id", 18),
        ("path", ".github/workflows/other.yml"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ],
)
def test_workflow_decision_rejects_nonmatching_authenticated_evidence(tmp_path, field, value):
    evidence = _evidence()
    for view in ("listed", "attempt", "latest"):
        evidence["workflow_runs"][0][view][field] = value
    assert _run(tmp_path, evidence) == "false"


def test_workflow_decision_rejects_name_only_or_branch_only_evidence(tmp_path):
    name_only = _run_record(workflow_id=18, branch="feature", head_sha="b" * 40)
    branch_only = _run_record(head_sha="b" * 40)
    assert _run(tmp_path, _evidence(name_only)) == "false"
    assert _run(tmp_path, _evidence(branch_only)) == "false"


def test_workflow_decision_rejects_pr_merge_sha_even_when_sha_matches(tmp_path):
    assert _run(tmp_path, _evidence(_run_record(event="pull_request"))) == "false"


def test_workflow_decision_requires_exact_latest_attempt_state(tmp_path):
    evidence = _evidence()
    entry = evidence["workflow_runs"][0]
    entry["attempt"]["run_attempt"] = 1
    assert _run(tmp_path, evidence) == "false"

    evidence = _evidence()
    evidence["workflow_runs"][0]["latest"]["run_attempt"] = 3
    evidence["workflow_runs"][0]["latest"]["status"] = "in_progress"
    evidence["workflow_runs"][0]["latest"]["conclusion"] = None
    assert _run(tmp_path, evidence) == "false"


def test_workflow_decision_does_not_fall_back_to_older_success(tmp_path):
    older_success = _run_record(40)
    newest_failure = _run_record(41, conclusion="failure")
    assert _run(tmp_path, _evidence(older_success, newest_failure)) == "false"


@pytest.mark.parametrize(
    "evidence",
    [
        {"current_run": _current_run(), "workflow_runs": []},
        [],
        {},
        {"current_run": _current_run(), "workflow_runs": [{"listed": _run_record()}]},
    ],
)
def test_workflow_decision_fails_closed_without_complete_evidence(tmp_path, evidence):
    assert _run(tmp_path, evidence) == "false"


@pytest.mark.parametrize("value", [True, 52.0, 0, -1])
def test_workflow_decision_rejects_loose_or_nonpositive_run_identity(tmp_path, value):
    evidence = _evidence()
    evidence["workflow_runs"][0]["listed"]["id"] = value
    assert _run(tmp_path, evidence) == "false"


@pytest.mark.parametrize(
    ("view", "field", "value"),
    [
        ("current_run", "id", True),
        ("current_run", "run_attempt", True),
        ("current_run", "workflow_id", True),
        ("attempt", "id", True),
        ("attempt", "run_attempt", True),
        ("latest", "workflow_id", True),
    ],
)
def test_workflow_decision_requires_strict_identity_in_every_api_view(
    tmp_path, view, field, value
):
    evidence = _evidence()
    target = (
        evidence["current_run"]
        if view == "current_run"
        else evidence["workflow_runs"][0][view]
    )
    target[field] = value
    assert _run(tmp_path, evidence) == "false"


def test_workflow_decision_fails_closed_for_malformed_or_duplicate_json(tmp_path):
    evidence_path = tmp_path / "workflow-runs.json"
    evidence_path.write_text('{"current_run":{},"current_run":{}}', encoding="utf-8")
    command = [
        sys.executable,
        str(SCRIPT),
        "--event-name",
        "push",
        "--ref",
        "refs/heads/main",
        "--default-branch",
        "main",
        "--repository-full-name",
        "acme/widgets",
        "--checkout-sha",
        SHA,
        "--current-run-id",
        "52",
        "--current-run-attempt",
        "1",
        "--workflow-runs-file",
        str(evidence_path),
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    assert result.stdout.strip() == "false"
    assert result.stderr == ""


def test_workflow_collects_authenticated_actions_evidence_without_custom_app_configuration():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "actions: read" in workflow
    assert "/actions/runs/" in workflow
    assert "/attempts/" in workflow
    assert "github.run_id" in workflow
    assert "github.run_attempt" in workflow
    assert "workflow-runs.json" in workflow
    assert "AQ_INTEGRATION_ATTESTATION_APP_ID" not in workflow
    assert "AQ_INTEGRATION_REQUIRED_CHECK_VERSION" not in workflow
    assert "agent-queue-integration.json" not in workflow


def test_workflow_routes_integration_push_once_and_preserves_exact_check_names():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "integration-attestation" in workflow
    assert "refs/heads/main" in workflow
    assert "refs/heads/aq/integration/" in workflow
    assert "github.event.pull_request.head.ref" in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "git rev-parse HEAD" in workflow
    for name in ("default", "migration-and-slow", "postgres-integration"):
        assert f"name: {name}" in workflow


def test_duplicate_pr_suppression_requires_same_repository_and_exact_generated_ref():
    assert _matching_pr(head_repository="acme/widgets", head_ref=CANDIDATE_REF) == "true"
    assert _matching_pr(head_repository="fork/widgets", head_ref=CANDIDATE_REF) == "false"
    assert _matching_pr(
        head_repository="acme/widgets", head_ref="aq/integration/untrusted"
    ) == "false"
