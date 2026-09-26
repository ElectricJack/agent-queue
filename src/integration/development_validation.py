"""Run one development validation command and say what its result *means*.

The development publisher parks a batch and files a repair when its selected
validation fails.  That is only right when tests ran and failed.  A run that
could not finish — it timed out, never got a test slot, lost its database,
was killed, or collected nothing — verified nothing about the batch, and a
repair for it sends a worker to fix code that is not broken (2026-09-16: a
dead validation database; 2026-09-24: 160 s queued for a test slot charged
to a 300 s budget).

So every run is classified:

``passed``          exit 0 and something was verified.
``failed``          tests ran and failed — the batch parks and a repair is
                    filed naming the failing tests.
``infrastructure``  could not finish validating.  The publisher defers the
                    batch to the next tick; no park, no repair.

The queue owns execution and reports queue and run durations separately.
The publisher never spawns a command or cancels one on viewer disconnection.
"""

from __future__ import annotations


from src.integration.development_result_parser import (
    FAILED,
    INFRASTRUCTURE,
    MAX_FAILING_TESTS,
    PASSED,
    classify_report,
    parse_pytest_output,
)
from src.jobs.adapters import finite_command
from src.jobs.policy import JobError

#: ``evidence.kind`` of the journal row recording a streak of deferrals.
DEFERRAL_KIND = "validation_deferred"
#: Consecutive infrastructure outcomes before the streak is surfaced to the
#: supervisor and to ``aq doctor --check integration.development_publisher_stalled``.
INFRA_ALERT_AFTER = 3

def classify(exit_code: int, output: str) -> tuple[str, str | None, list[dict]]:
    """Compatibility classifier for development validation's exit-5 deferrals."""
    return classify_report(exit_code, parse_pytest_output(output), legacy_no_tests=True)


def _detail(outcome, reason, exit_code, check) -> str:
    if outcome == PASSED:
        return "passed"
    if reason in {"timeout", "run_timeout"}:
        return (
            f"timed out after running {check['run_seconds']:.0f}s "
            f"(budget {check['timeout_seconds']}s, plus {check['slot_wait_seconds']:.0f}s "
            "queued for a test slot)"
        )
    if reason in {"slot_unavailable", "queue_timeout"}:
        return (
            f"no test slot came free within {check['slot_wait_seconds']:.0f}s "
            f"(bound {check['slot_wait_bound_seconds']}s)"
        )
    failing = check["failing_tests"]
    if outcome == FAILED and failing:
        return f"{len(failing)} failing test(s), first {failing[0]['id']}"
    if reason is None:
        return f"exited {exit_code}"
    return f"{reason.replace('_', ' ')} (exit {exit_code})"


async def run_check(
    command: str,
    *,
    cwd,
    timeout_seconds: float,
    slot_wait_seconds: float,
    job_client=None,
    project_id=None,
    operation_id=None,
    input_ref=None,
    idempotency_key=None,
    poll_seconds: float = 1.0,
) -> dict:
    """Submit a finite preset and consume the queue's immutable result.

    Cancellation or loss of this caller leaves execution owned by the queue.
    Unsupported commands and disabled admission defer validation; there is no
    shell fallback, including after an ambiguous submission response.
    """
    job_id = None
    try:
        if job_client is None:
            raise JobError("jobs.disabled")
        preset, argv = finite_command(command)
        job = await job_client.submit(
            project_id=project_id, operation_id=operation_id, store=str(cwd),
            input_ref=input_ref, preset=preset, argv=argv,
            idempotency_key=idempotency_key,
            queue_seconds=slot_wait_seconds, run_seconds=timeout_seconds,
        )
        job_id = job["id"]
        job = await job_client.wait(job, poll_seconds=poll_seconds)
        result = job.get("result")
        if not result:
            raise JobError("jobs.result_missing")
        outcome = result["outcome"]
        reason = result.get("infra_reason")
        if outcome in {"lost", "cancelled"}:
            outcome, reason = INFRASTRUCTURE, outcome
        # An attestation must describe the candidate the publisher submitted.
        # A modified tracked input or a different HEAD cannot be published.
        if outcome == PASSED and (
            result.get("input_ref") != input_ref
            or result.get("input_stability") != "stable"
        ):
            outcome, reason = INFRASTRUCTURE, "snapshot_modified"
        exit_code = result.get("exit_code")
        queued, ran = result.get("queue_seconds") or 0, result.get("run_seconds") or 0
    except JobError as exc:
        result = {}
        outcome, reason, exit_code, queued, ran = INFRASTRUCTURE, str(exc), None, 0, 0
    check = {
        "command": command, "job_id": job_id,
        "exit_code": exit_code, "outcome": outcome,
        "infra_reason": reason if outcome == INFRASTRUCTURE else None,
        "duration_seconds": round(queued + ran, 3),
        "slot_wait_seconds": round(queued, 3), "run_seconds": round(ran, 3),
        "timeout_seconds": timeout_seconds, "slot_wait_bound_seconds": slot_wait_seconds,
        "failing_tests": result.get("failing_tests") or [],
        "summary": result.get("summary"),
        "omitted_failure_count": result.get("omitted_failure_count", 0),
        "parser_source": result.get("parser_source"),
        "result_hash": result.get("result_hash"),
        "input_ref": result.get("input_ref"),
        "input_mode": result.get("input_mode"),
        "output": result.get("excerpt", ""),
    }
    check["detail"] = _detail(outcome, check["infra_reason"], exit_code, check)
    return check


def conclude(checks: list[dict]) -> str:
    """One conclusion for a validation: a real failure outranks an outage."""
    if not checks:
        return "not_run"
    outcomes = {check.get("outcome") for check in checks}
    if FAILED in outcomes:
        return FAILED
    if INFRASTRUCTURE in outcomes:
        return INFRASTRUCTURE
    return PASSED


def failing_tests(evidence: dict) -> list[dict]:
    """Every failing test an evidence record names, parsing older records.

    Rows parked before checks carried ``failing_tests`` still hold their
    output tail, so the ids are recovered from it.
    """
    found, seen = [], set()
    for check in (evidence or {}).get("checks") or []:
        if not isinstance(check, dict):
            continue
        tests = check.get("failing_tests")
        if tests is None:
            tests = parse_pytest_output(str(check.get("output") or "")).failing
        for test in tests:
            if isinstance(test, dict) and test.get("id") and test["id"] not in seen:
                seen.add(test["id"])
                found.append({"id": test["id"], "reason": test.get("reason") or ""})
    return found[:MAX_FAILING_TESTS]


def reclassify(evidence: dict) -> str:
    """The conclusion a stored validation record deserves under these rules.

    Records written before this module carry only ``exit_code`` and
    ``output``; a batch they parked for a timeout or an outage never failed
    a test.
    """
    checks = []
    for check in (evidence or {}).get("checks") or []:
        if not isinstance(check, dict):
            continue
        outcome = check.get("outcome")
        if outcome is None:
            code = check.get("exit_code")
            if not isinstance(code, int):
                continue
            outcome = classify(code, str(check.get("output") or ""))[0]
        checks.append({"outcome": outcome})
    return conclude(checks)
