"""Result v1 rendering; observed exit and integrity are authoritative."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from src.integration.development_result_parser import PARSER_VERSION, PytestReport, classify_report

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def clean(text: str) -> str:
    return "".join(c for c in _ANSI.sub("", text) if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def bounded(text: str, size: int) -> str:
    data = clean(text).encode("utf-8", "replace")
    if len(data) <= size:
        return data.decode()
    marker = b"\n[omitted]\n"[:size]
    return data[: max(0, size - len(marker))].decode("utf-8", "ignore") + marker.decode()


def build_result(job: dict, completion: dict | None, tail: bytes = b"") -> dict:
    receipt = completion or {}
    report = (
        PytestReport(**receipt["report"])
        if receipt.get("report")
        else PytestReport([], None, False)
    )
    outcome, reason, failing = classify_report(
        receipt.get("exit_code"),
        report,
        output_integrity=not receipt.get("output_store_failed", False),
        cancelled=receipt.get("cancelled", False),
    )
    if receipt.get("infra_reason"):
        outcome, reason = "infrastructure", receipt["infra_reason"]
    if completion is None:
        outcome, reason = "lost", "missing_receipt"
    state = {
        "passed": "succeeded",
        "failed": "failed",
        "infrastructure": "failed",
        "cancelled": "cancelled",
        "lost": "lost",
    }[outcome]
    header = bounded(
        f"Job {job['id']} ({job['preset']}): {outcome}; exit={receipt.get('exit_code')}\n"
        f"Output: aq job logs {job['id']} (retained ranges only)\n",
        512,
    )
    failures = "".join(bounded(f"{f['id']}: {f['reason']}\n", 1024) for f in failing)
    failures = bounded(failures, 4096)
    initial = bounded(receipt.get("initial_errors", ""), 1024)
    prefix = header + failures + initial
    excerpt = prefix + bounded(tail.decode("utf-8", "replace"), 8192 - len(prefix.encode()))
    result = {
        "schema_version": 1,
        "job_id": job["id"],
        "preset": job["preset"],
        "input_mode": job["input_mode"],
        "input_ref": receipt.get("input_ref", job.get("input_ref")),
        "input_stability": receipt.get("input_stability", "unverified"),
        "state": state,
        "exit_code": receipt.get("exit_code"),
        "signal": receipt.get("signal"),
        "outcome": outcome,
        "infra_reason": reason,
        "queue_seconds": receipt.get("queue_seconds"),
        "run_seconds": receipt.get("run_seconds"),
        "summary": report.summary,
        "failing_tests": failing,
        "excerpt": excerpt,
        "output_bytes_seen": receipt.get("output_bytes_seen", 0),
        "output_bytes_retained": receipt.get("output_bytes_retained", 0),
        "truncated": receipt.get("truncated", False) or report.truncated,
        "omitted_failure_count": report.omitted_failure_count,
        "output_ref": job["id"],
        "parser_version": PARSER_VERSION,
        "parser_source": report.source,
        "artifact_status": report.artifact_status,
    }
    result["result_hash"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def report_json(report):
    return asdict(report)
