"""Finite command adapters. Shell syntax is never submitted to the runner."""

from __future__ import annotations

import shlex

from src.jobs.policy import JobError


def finite_command(command: str | list[str]) -> tuple[str, list[str]]:
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    prefixes = (
        (("aq", "test"), "test"),
        (("pytest",), "test"),
        (("python", "-m", "pytest"), "test"),
        (("python3", "-m", "pytest"), "test"),
        (("ruff", "check"), "lint"),
        (("python", "-m", "ruff", "check"), "lint"),
        (("python3", "-m", "ruff", "check"), "lint"),
        (("npm", "run", "build"), "build"),
    )
    for prefix, preset in prefixes:
        if tuple(argv[: len(prefix)]) == prefix:
            args = argv[len(prefix) :]
            if any(arg in {";", "&&", "||", "|", ">", ">>", "<", "&"} for arg in args):
                raise JobError("jobs.preset_denied")
            return preset, args
    raise JobError("jobs.preset_denied")


def validation_evidence(command: str, job: dict, policy) -> dict:
    """Project immutable result v1 into the publisher's existing evidence shape."""
    result = job.get("result") or {}
    outcome = result.get("outcome", "lost")
    if outcome not in {"passed", "failed", "infrastructure"}:
        outcome = "infrastructure"
    if result.get("input_stability") != "stable" or result.get("input_ref") != job.get("input_ref"):
        outcome = "infrastructure"
        reason = result.get("infra_reason") or "snapshot_unverified"
    else:
        reason = result.get("infra_reason") or (
            result.get("outcome") if result.get("outcome") in {"lost", "cancelled"} else None
        )
    queue, run = result.get("queue_seconds") or 0, result.get("run_seconds") or 0
    return {
        "command": command,
        "job_id": job["id"],
        "result_hash": result.get("result_hash"),
        "input_ref": result.get("input_ref"),
        "input_stability": result.get("input_stability"),
        "exit_code": result.get("exit_code"),
        "outcome": outcome,
        "infra_reason": reason if outcome == "infrastructure" else None,
        "duration_seconds": queue + run,
        "slot_wait_seconds": queue,
        "run_seconds": run,
        "timeout_seconds": policy.timeout_seconds,
        "slot_wait_bound_seconds": policy.slot_wait_seconds,
        "failing_tests": result.get("failing_tests") or [],
        "summary": result.get("summary"),
        "omitted_failure_count": result.get("omitted_failure_count", 0),
        "output": result.get("excerpt", ""),
        "detail": reason or outcome,
    }
