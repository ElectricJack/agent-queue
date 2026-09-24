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
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from src.resources.slot_report import REPORT_ENV, WAIT_TIMEOUT_ENV, read_slot_wait

PASSED = "passed"
FAILED = "failed"
INFRASTRUCTURE = "infrastructure"

#: ``evidence.kind`` of the journal row recording a streak of deferrals.
DEFERRAL_KIND = "validation_deferred"
#: Consecutive infrastructure outcomes before the streak is surfaced to the
#: supervisor and to ``aq doctor --check integration.development_publisher_stalled``.
INFRA_ALERT_AFTER = 3

#: Characters of combined output kept on each check (the tail).
OUTPUT_TAIL_CHARS = 8000
#: Bytes of output kept in memory while running, for finding failing tests.
_PARSE_TAIL_BYTES = 256 * 1024
#: Failing test ids kept per check.
MAX_FAILING_TESTS = 100

#: Exit codes that say nothing was verified.  ``3``/``4``/``5`` are pytest's
#: internal error, usage error and "no tests collected" (``aq test`` also
#: uses ``4`` for a missing path or test DSN); ``75`` is ``aq test``'s
#: EX_TEMPFAIL when no slot came free; ``124`` is ``timeout(1)``'s and the
#: publisher's own timeout code; ``126``/``127`` mean the command could not
#: be executed at all.
_EXIT_REASONS = {
    3: "test_runner_error",
    4: "test_runner_error",
    5: "no_tests_collected",
    75: "slot_unavailable",
    124: "timeout",
    126: "command_unavailable",
    127: "command_unavailable",
}
#: Signals that mean something outside the run stopped it.
_KILL_SIGNALS = {signal.SIGHUP, signal.SIGINT, signal.SIGKILL, signal.SIGTERM}

#: Failure text that names the validation environment rather than the code
#: under test: the test database, the box, the wrapper's container.
_INFRASTRUCTURE_PATTERNS = (
    r"connection refused",
    r"connect call failed",
    r"could not connect to server",
    r"too many (?:clients|connections)",
    r"remaining connection slots are reserved",
    r"the database system is (?:starting up|shutting down|in recovery mode)",
    r"server closed the connection unexpectedly",
    r"terminating connection due to administrator command",
    r"CannotConnectNowError",
    r"TooManyConnectionsError",
    r"ConnectionDoesNotExistError",
    r"connection was closed in the middle of operation",
    r"no space left on device",
    r"too many open files",
    r"POSTGRES_TEST_DSN is not set",
    r"no test slot free",
    r"Cannot connect to the Docker daemon",
    r"Error response from daemon",
)
INFRASTRUCTURE_SIGNATURES = re.compile("|".join(_INFRASTRUCTURE_PATTERNS), re.IGNORECASE)

#: pytest's short test summary: ``FAILED <node id> - <reason>``.  The node id
#: must be a ``.py`` path, so captured output and log lines that merely start
#: with FAILED/ERROR are never taken for tests.
_FAILURE_LINE = re.compile(
    r"^(?:FAILED|ERROR) (?P<id>[^\s:]+\.py(?:::.*?)?)(?: - (?P<reason>.*))?$"
)
_SUMMARY_LINE = re.compile(
    r"^=*\s*(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*) in [\d.]+s\b.*$", re.MULTILINE
)
_SUMMARY_COUNT = re.compile(r"(\d+) ([a-z]+)")
_NO_TESTS_RAN = re.compile(r"^=*\s*no tests ran\b", re.MULTILINE)
_COUNT_KEYS = {"error": "errors", "warning": "warnings"}


@dataclass(frozen=True)
class PytestReport:
    #: ``[{"id": ..., "reason": ...}]`` from pytest's short test summary.
    failing: list[dict]
    #: The final ``N failed, M passed`` counts, or ``None`` when absent.
    summary: dict | None
    no_tests_ran: bool


def parse_pytest_output(text: str) -> PytestReport:
    """Failing test ids and the final counts from pytest output, if any."""
    failing, seen = [], set()
    for line in text.splitlines():
        match = _FAILURE_LINE.match(line.strip())
        if match is None:
            continue
        test_id = match.group("id").strip()
        if test_id in seen:
            continue
        seen.add(test_id)
        failing.append({"id": test_id, "reason": (match.group("reason") or "").strip()})
    summary = None
    lines = _SUMMARY_LINE.findall(text)
    if lines:
        summary = {
            _COUNT_KEYS.get(word, word): int(count)
            for count, word in _SUMMARY_COUNT.findall(lines[-1])
        }
    return PytestReport(failing, summary, bool(_NO_TESTS_RAN.search(text)))


def _killed_by(code: int) -> bool:
    if code < 0:
        return -code in _KILL_SIGNALS
    return code > 128 and code - 128 in _KILL_SIGNALS


def classify(exit_code: int, output: str) -> tuple[str, str | None, list[dict]]:
    """``(outcome, infra_reason, failing_tests)`` for one finished command.

    Only evidence that tests ran and failed makes a ``failed``: a failing
    test whose reason is not an environment outage, or — for a command that
    prints nothing pytest-shaped — a nonzero exit that no infrastructure
    signature explains.  Everything that verified nothing is
    ``infrastructure``.
    """
    report = parse_pytest_output(output)
    failing = report.failing[:MAX_FAILING_TESTS]
    if exit_code in _EXIT_REASONS:
        return INFRASTRUCTURE, _EXIT_REASONS[exit_code], failing
    if _killed_by(exit_code):
        return INFRASTRUCTURE, "killed", failing
    if report.no_tests_ran:
        return INFRASTRUCTURE, "no_tests_collected", failing
    if exit_code == 0:
        return PASSED, None, []
    if failing:
        if all(INFRASTRUCTURE_SIGNATURES.search(test["reason"]) for test in failing):
            return INFRASTRUCTURE, "infrastructure_error", failing
        return FAILED, None, failing
    if INFRASTRUCTURE_SIGNATURES.search(output):
        return INFRASTRUCTURE, "infrastructure_error", []
    return FAILED, None, []


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

    async def pump():
        while chunk := await process.stdout.read(65536):
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
    try:
        await asyncio.wait_for(reader, 5)
    except TimeoutError:
        reader.cancel()
    finished = time.time()
    wait = read_slot_wait(report_path, now=finished)
    report_path.unlink(missing_ok=True)
    output = buffer.decode(errors="replace")
    exit_code = {"timeout": 124, "slot_unavailable": 75}.get(stopped, process.returncode)
    outcome, reason, failing = classify(exit_code, output)
    if stopped:
        outcome, reason = INFRASTRUCTURE, stopped
    if wait.timed_out and outcome == INFRASTRUCTURE:
        reason = "slot_unavailable"
    duration = finished - started
    summary = parse_pytest_output(output).summary
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
