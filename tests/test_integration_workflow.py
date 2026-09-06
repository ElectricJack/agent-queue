from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.integration.ci import ATTESTATION_CHECK_NAME, AttestationPayload


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-integration-attestation.py"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SHA = "a" * 40


def _trust() -> dict:
    return {
        "schema": "aq.integration-trust.v1",
        "canonical_repository_id": "repo-config-1",
        "repository_id": 303,
        "full_name": "acme/widgets",
        "ci_producer_app_id": 404,
        "attestation_app_id": 101,
        "attestation_name": ATTESTATION_CHECK_NAME,
        "required_checks": {"version": "checks-v1", "names": ["Tests (default)"]},
    }


def _payload() -> AttestationPayload:
    return AttestationPayload.model_validate(
        {
            "schema": "aq.integration-attestation.v1",
            "canonical_repository_id": "repo-config-1",
            "repository_id": 303,
            "ci_producer_app_id": 404,
            "attestation_app_id": 101,
            "head_sha": SHA,
            "required_check_set_version": "checks-v1",
            "checks": [
                {
                    "name": "Tests (default)",
                    "check_run_id": 11,
                    "check_suite_id": 21,
                    "producer_app_id": 404,
                    "head_sha": SHA,
                    "conclusion": "success",
                }
            ],
            "workflow_runs": [
                {
                    "workflow_run_id": 31,
                    "run_attempt": 1,
                    "check_suite_id": 21,
                    "head_sha": SHA,
                    "conclusion": "success",
                }
            ],
        }
    )


def _record(record_id: object = 7, *, conclusion: str = "success") -> dict:
    payload = _payload()
    return {
        "id": record_id,
        "name": ATTESTATION_CHECK_NAME,
        "app": {"id": 101},
        "head_sha": SHA,
        "status": "completed",
        "conclusion": conclusion,
        "external_id": payload.external_id,
        "output": {"text": payload.canonical_bytes().decode("ascii")},
    }


def _run(tmp_path: Path, *, trust=None, records=None, **changes) -> str:
    trust_path = tmp_path / "trust.json"
    records_path = tmp_path / "records.json"
    trust_path.write_text(json.dumps(_trust() if trust is None else trust))
    records_path.write_text(json.dumps([_record()] if records is None else records))
    args = {
        "event-name": "push",
        "ref": "refs/heads/main",
        "repository-id": "303",
        "checkout-sha": SHA,
        "integration-app-id": "101",
        "required-check-version": "checks-v1",
    }
    args.update(changes)
    command = [sys.executable, str(SCRIPT)]
    for key, value in args.items():
        command.extend((f"--{key}", value))
    command.extend(("--trust-file", str(trust_path), "--records-file", str(records_path)))
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def test_workflow_decision_accepts_only_exact_main_attestation(tmp_path):
    assert _run(tmp_path) == "true"


@pytest.mark.parametrize(
    "changes",
    [
        {"event-name": "pull_request"},
        {"ref": "refs/heads/aq/integration/batch"},
        {"repository-id": "304"},
        {"checkout-sha": "b" * 40},
        {"integration-app-id": "102"},
        {"required-check-version": "checks-v2"},
    ],
)
def test_workflow_decision_fails_closed_for_identity_mismatch(tmp_path, changes):
    assert _run(tmp_path, **changes) == "false"


@pytest.mark.parametrize(
    "records",
    [
        [],
        [_record(conclusion="neutral")],
        [_record(), _record(8, conclusion="neutral")],
        [_record(record_id=True)],
    ],
)
def test_workflow_decision_fails_closed_without_newest_valid_record(tmp_path, records):
    assert _run(tmp_path, records=records) == "false"


def test_workflow_decision_fails_closed_for_malformed_or_duplicate_json(tmp_path):
    trust_path = tmp_path / "trust.json"
    records_path = tmp_path / "records.json"
    trust_path.write_text('{"schema":"aq.integration-trust.v1","schema":"duplicate"}')
    records_path.write_text("not json")
    command = [
        sys.executable,
        str(SCRIPT),
        "--event-name", "push",
        "--ref", "refs/heads/main",
        "--repository-id", "303",
        "--checkout-sha", SHA,
        "--integration-app-id", "101",
        "--required-check-version", "checks-v1",
        "--trust-file", str(trust_path),
        "--records-file", str(records_path),
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    assert result.stdout.strip() == "false"
    assert result.stderr == ""


def test_workflow_routes_integration_push_once_and_preserves_exact_check_names():
    workflow = WORKFLOW.read_text()
    assert "integration-attestation" in workflow
    assert "refs/heads/main" in workflow
    assert "refs/heads/aq/integration/" in workflow
    assert "github.event.pull_request.head.ref" in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "git rev-parse HEAD" in workflow
    for name in ("default", "migration-and-slow", "postgres-integration"):
        assert f"name: {name}" in workflow
