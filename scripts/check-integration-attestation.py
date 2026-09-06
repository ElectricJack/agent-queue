#!/usr/bin/env python3
"""Fail-closed hosted-CI decision for an exact integration attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ATTESTATION_NAME = "Agent Queue Integration Attestation"
TRUST_KEYS = {
    "schema",
    "canonical_repository_id",
    "repository_id",
    "full_name",
    "ci_producer_app_id",
    "attestation_app_id",
    "attestation_name",
    "required_checks",
}
PAYLOAD_KEYS = {
    "schema",
    "canonical_repository_id",
    "repository_id",
    "ci_producer_app_id",
    "attestation_app_id",
    "head_sha",
    "required_check_set_version",
    "checks",
    "workflow_runs",
}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _load(path: str) -> Any:
    source = Path(path)
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("input too large")
    return json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _valid_trust(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != TRUST_KEYS:
        return False
    required = value.get("required_checks")
    names = required.get("names") if isinstance(required, dict) else None
    return bool(
        value.get("schema") == "aq.integration-trust.v1"
        and isinstance(value.get("canonical_repository_id"), str)
        and value["canonical_repository_id"]
        and _positive_int(value.get("repository_id"))
        and isinstance(value.get("full_name"), str)
        and value["full_name"].count("/") == 1
        and _positive_int(value.get("ci_producer_app_id"))
        and _positive_int(value.get("attestation_app_id"))
        and value["ci_producer_app_id"] != value["attestation_app_id"]
        and value.get("attestation_name") == ATTESTATION_NAME
        and isinstance(required, dict)
        and set(required) == {"version", "names"}
        and isinstance(required.get("version"), str)
        and required["version"]
        and isinstance(names, list)
        and names
        and all(isinstance(name, str) and name for name in names)
        and len(set(names)) == len(names)
    )


def _valid_payload(payload: object, trust: dict[str, Any], head_sha: str) -> bool:
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        return False
    checks = payload.get("checks")
    workflows = payload.get("workflow_runs")
    if not isinstance(checks, list) or not checks or not isinstance(workflows, list) or not workflows:
        return False
    names: list[str] = []
    suites: list[int] = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {
            "name", "check_run_id", "check_suite_id", "producer_app_id", "head_sha", "conclusion"
        }:
            return False
        if not all(_positive_int(check.get(key)) for key in ("check_run_id", "check_suite_id", "producer_app_id")):
            return False
        if (
            check.get("head_sha") != head_sha
            or check.get("conclusion") != "success"
            or check.get("producer_app_id") != trust["ci_producer_app_id"]
            or not isinstance(check.get("name"), str)
        ):
            return False
        names.append(check["name"])
        suites.append(check["check_suite_id"])
    workflow_suites: list[int] = []
    for workflow in workflows:
        if not isinstance(workflow, dict) or set(workflow) != {
            "workflow_run_id", "run_attempt", "check_suite_id", "head_sha", "conclusion"
        }:
            return False
        if not all(_positive_int(workflow.get(key)) for key in ("workflow_run_id", "run_attempt", "check_suite_id")):
            return False
        if workflow.get("head_sha") != head_sha or workflow.get("conclusion") != "success":
            return False
        workflow_suites.append(workflow["check_suite_id"])
    return bool(
        payload.get("schema") == "aq.integration-attestation.v1"
        and payload.get("canonical_repository_id") == trust["canonical_repository_id"]
        and payload.get("repository_id") == trust["repository_id"]
        and payload.get("ci_producer_app_id") == trust["ci_producer_app_id"]
        and payload.get("attestation_app_id") == trust["attestation_app_id"]
        and payload.get("head_sha") == head_sha
        and payload.get("required_check_set_version") == trust["required_checks"]["version"]
        and names == trust["required_checks"]["names"]
        and len(names) == len(set(names))
        and len(suites) == len(set(suites))
        and set(suites) == set(workflow_suites)
        and len(workflow_suites) == len(set(workflow_suites))
    )


def decide(args: argparse.Namespace) -> bool:
    if args.event_name != "push" or args.ref != "refs/heads/main":
        return False
    trust = _load(args.trust_file)
    records = _load(args.records_file)
    if not _valid_trust(trust) or not isinstance(records, list):
        return False
    repository_id = int(args.repository_id)
    app_id = int(args.integration_app_id)
    if (
        trust["repository_id"] != repository_id
        or trust["attestation_app_id"] != app_id
        or trust["required_checks"]["version"] != args.required_check_version
    ):
        return False
    trusted: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        app = record.get("app")
        if (
            record.get("name") == ATTESTATION_NAME
            and isinstance(app, dict)
            and _positive_int(app.get("id"))
            and app.get("id") == app_id
        ):
            if not _positive_int(record.get("id")):
                return False
            trusted.append((record["id"], record))
    if not trusted:
        return False
    record = max(trusted, key=lambda item: item[0])[1]
    if (
        record.get("status") != "completed"
        or record.get("conclusion") != "success"
        or record.get("head_sha") != args.checkout_sha
        or not isinstance(record.get("output"), dict)
        or not isinstance(record["output"].get("text"), str)
    ):
        return False
    raw = record["output"]["text"].encode("utf-8")
    payload = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(payload, dict) or _canonical(payload) != raw:
        return False
    digest = "aq-attestation-v1:" + hashlib.sha256(raw).hexdigest()
    return record.get("external_id") == digest and _valid_payload(payload, trust, args.checkout_sha)


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in (
        "event-name", "ref", "repository-id", "checkout-sha", "integration-app-id",
        "required-check-version", "trust-file", "records-file",
    ):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    try:
        result = decide(args)
    except Exception:
        result = False
    print("true" if result else "false")


if __name__ == "__main__":
    main()
