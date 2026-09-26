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

The run budget (``timeout_seconds``) covers the run only.  Time the command
spends queued for a slot is read from the report ``aq test`` writes
(:mod:`src.resources.slot_report`) and bounded separately by
``slot_wait_seconds``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from src.integration.development_result_parser import (
    FAILED,
    INFRASTRUCTURE,
    MAX_FAILING_TESTS,
    PASSED,
    PytestOutputParser,
    classify_report,
    parse_pytest_output,
)
from src.resources.slot_report import REPORT_ENV, WAIT_TIMEOUT_ENV, read_slot_wait

#: ``evidence.kind`` of the journal row recording a streak of deferrals.
DEFERRAL_KIND = "validation_deferred"
#: Consecutive infrastructure outcomes before the streak is surfaced to the
#: supervisor and to ``aq doctor --check integration.development_publisher_stalled``.
INFRA_ALERT_AFTER = 3

#: Characters of combined output kept on each check (the tail).
OUTPUT_TAIL_CHARS = 8000
#: Bytes of output kept in memory while running, for the display tail only.
_PARSE_TAIL_BYTES = 256 * 1024


def classify(exit_code: int, output: str) -> tuple[str, str | None, list[dict]]:
    """Compatibility classifier for development validation's exit-5 deferrals."""
    return classify_report(exit_code, parse_pytest_output(output), legacy_no_tests=True)


def _detail(outcome, reason, exit_code, check) -> str:
    if outcome == PASSED:
        return "passed"
    if reason == "timeout":
        return (
            f"timed out after running {check['run_seconds']:.0f}s "
            f"(budget {check['timeout_seconds']}s, plus {check['slot_wait_seconds']:.0f}s "
            "queued for a test slot)"
        )
    if reason == "slot_unavailable":
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


def _kill(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_check(
    command: str,
    *,
    cwd,
    timeout_seconds: float,
    slot_wait_seconds: float,
    report_path,
    poll_seconds: float = 1.0,
    slot_grace_seconds: float = 10.0,
) -> dict:
    """Run *command* under ``bash -c`` and return its classified evidence.

    The command runs in its own session so a timeout can kill its whole
    process group.  Its output is collected as it arrives, so a run that is
    stopped still leaves its tail behind.  ``aq test`` inside the command
    bounds its own slot wait via :data:`WAIT_TIMEOUT_ENV`; the publisher
    stops the command itself once the reported wait passes that bound by
    *slot_grace_seconds*.
    """
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    env = {
        **os.environ,
        REPORT_ENV: str(report_path),
        WAIT_TIMEOUT_ENV: str(max(0, int(slot_wait_seconds))),
    }
    started = time.time()
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        command,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    buffer = bytearray()
    parser = PytestOutputParser()

    async def pump():
        while chunk := await process.stdout.read(65536):
            parser.feed(chunk)
            buffer.extend(chunk)
            if len(buffer) > _PARSE_TAIL_BYTES:
                del buffer[: len(buffer) - _PARSE_TAIL_BYTES]

    reader = asyncio.ensure_future(pump())
    exited = asyncio.ensure_future(process.wait())
    stopped = None
    try:
        while True:
            done, _pending = await asyncio.wait({exited}, timeout=poll_seconds)
            if done:
                break
            now = time.time()
            wait = read_slot_wait(report_path, now=now)
            if wait.total_seconds > slot_wait_seconds + slot_grace_seconds:
                stopped = "slot_unavailable"
            elif (now - started) - wait.total_seconds > timeout_seconds:
                stopped = "timeout"
            if stopped:
                _kill(process)
                await exited
                break
    except asyncio.CancelledError:
        _kill(process)
        await process.wait()
        reader.cancel()
        report_path.unlink(missing_ok=True)
        raise
    # Killing the group closes every writer; a grandchild that escaped the
    # group must not hold the evidence hostage.
    output_integrity = True
    try:
        await asyncio.wait_for(reader, 5)
    except TimeoutError:
        reader.cancel()
        output_integrity = False
    finished = time.time()
    wait = read_slot_wait(report_path, now=finished)
    report_path.unlink(missing_ok=True)
    output = buffer.decode(errors="replace")
    exit_code = {"timeout": 124, "slot_unavailable": 75}.get(stopped, process.returncode)
    report = parser.finish()
    outcome, reason, failing = classify_report(
        exit_code, report, output_integrity=output_integrity, legacy_no_tests=True
    )
    if stopped:
        outcome, reason = INFRASTRUCTURE, stopped
    if wait.timed_out and outcome == INFRASTRUCTURE:
        reason = "slot_unavailable"
    duration = finished - started
    summary = report.summary
    check = {
        "command": command,
        "exit_code": exit_code,
        "outcome": outcome,
        "infra_reason": reason if outcome == INFRASTRUCTURE else None,
        "duration_seconds": round(duration, 3),
        "slot_wait_seconds": round(wait.total_seconds, 3),
        "run_seconds": round(max(0.0, duration - wait.total_seconds), 3),
        "timeout_seconds": timeout_seconds,
        "slot_wait_bound_seconds": slot_wait_seconds,
        "failing_tests": failing,
        "summary": summary,
        "omitted_failure_count": report.omitted_failure_count,
        "parser_error": report.parser_error,
        "output": output[-OUTPUT_TAIL_CHARS:],
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
