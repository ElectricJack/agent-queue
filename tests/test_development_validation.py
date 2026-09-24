"""Development validation: "tests failed" is not "could not finish validating".

2026-09-24: a batch whose selected validation passes (119 passed in 209 s)
was parked as "selected validation failed" and a repair was filed, because
the ~160 s the command spent queued for a test slot was charged to the
300 s validation budget.  The output was discarded on timeout, so nothing
said what had "failed".  These tests pin the split: the budget covers the
run, queueing is bounded on its own, and only a run in which tests actually
failed parks a batch.
"""

from __future__ import annotations

import time

import pytest

from src.integration.development_validation import (
    FAILED,
    INFRASTRUCTURE,
    PASSED,
    classify,
    parse_pytest_output,
    run_check,
)


def _slot_command(*, wait: float, run: float, code: int = 0, echo: str = "") -> str:
    """A command that queues like ``aq test`` does, then "runs" for *run* seconds."""
    return (
        'python3 -c "import json, sys, time; '
        "open(sys.argv[1], 'a').write(json.dumps({'event': 'waiting', 'at': time.time()}) + '\\n')"
        '" "$AQ_TEST_SLOT_REPORT"; '
        f"sleep {wait}; "
        'python3 -c "import json, sys, time; '
        "open(sys.argv[1], 'a').write(json.dumps({'event': 'acquired', 'at': time.time(), "
        f"'waited': {wait}, 'slot': 0}}) + '\\n')"
        '" "$AQ_TEST_SLOT_REPORT"; '
        f"{f'echo {echo}; ' if echo else ''}"
        f"sleep {run}; exit {code}"
    )


async def _run(command, tmp_path, *, timeout=1, slot_wait=5, grace=0.0):
    return await run_check(
        command,
        cwd=tmp_path,
        timeout_seconds=timeout,
        slot_wait_seconds=slot_wait,
        report_path=tmp_path / "slot.jsonl",
        poll_seconds=0.05,
        slot_grace_seconds=grace,
    )


# -- the run budget ---------------------------------------------------------


async def test_slot_wait_is_not_charged_to_the_run_budget(tmp_path):
    # 1.5 s queued + 0.3 s running against a 1 s budget: the run fits.
    check = await _run(_slot_command(wait=1.5, run=0.3), tmp_path, timeout=1)
    assert check["outcome"] == PASSED
    assert check["exit_code"] == 0
    assert check["slot_wait_seconds"] >= 1.4
    assert check["duration_seconds"] > 1.0
    assert check["run_seconds"] < 1.0


async def test_a_slot_wait_past_its_own_bound_is_infrastructure(tmp_path):
    started = time.monotonic()
    check = await _run(_slot_command(wait=30, run=0), tmp_path, timeout=60, slot_wait=1)
    assert time.monotonic() - started < 10
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == "slot_unavailable"
    assert check["failing_tests"] == []


async def test_a_timeout_mid_run_is_infrastructure_and_keeps_the_output(tmp_path):
    check = await _run("echo collected 119 items; sleep 30", tmp_path, timeout=1)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == "timeout"
    assert check["exit_code"] == 124
    # The old runner replaced everything with "validation timed out".
    assert "collected 119 items" in check["output"]
    assert "timed out" in check["detail"]


async def test_aq_test_giving_up_on_a_slot_is_infrastructure(tmp_path):
    check = await _run("echo 'aq test: no test slot free after 600s'; exit 75", tmp_path)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == "slot_unavailable"


async def test_a_killed_process_is_infrastructure(tmp_path):
    check = await _run("echo started; kill -KILL $$", tmp_path)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == "killed"
    assert "started" in check["output"]


async def test_a_real_failure_names_the_failing_test(tmp_path):
    output = (
        "FAILED tests/test_development_integration.py::test_batch - "
        "AssertionError: assert 'parked' == 'delivered'\n"
        "1 failed, 118 passed in 209.00s"
    )
    check = await _run(f"printf '%s\\n' \"{output}\"; exit 1", tmp_path)
    assert check["outcome"] == FAILED
    assert check["infra_reason"] is None
    assert [t["id"] for t in check["failing_tests"]] == [
        "tests/test_development_integration.py::test_batch"
    ]
    assert check["summary"] == {"failed": 1, "passed": 118}


async def test_the_evidence_records_command_timing_and_exit(tmp_path):
    check = await _run("echo hi", tmp_path)
    assert check["command"] == "echo hi"
    assert check["exit_code"] == 0
    assert check["outcome"] == PASSED
    assert check["slot_wait_seconds"] == 0.0
    assert check["run_seconds"] == pytest.approx(check["duration_seconds"])
    assert check["output"].strip() == "hi"


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (5, "no_tests_collected"),
        (124, "timeout"),
        (75, "slot_unavailable"),
        (127, "command_unavailable"),
        (126, "command_unavailable"),
        (137, "killed"),
        (143, "killed"),
        (-9, "killed"),
        (3, "test_runner_error"),
        (4, "test_runner_error"),
    ],
)
def test_exit_codes_that_mean_nothing_was_verified(code, reason):
    outcome, infra_reason, failing = classify(code, "")
    assert (outcome, infra_reason, failing) == (INFRASTRUCTURE, reason, [])


def test_no_tests_ran_is_infrastructure_even_with_a_zero_exit():
    assert classify(0, "no tests ran in 0.12s")[:2] == (INFRASTRUCTURE, "no_tests_collected")


def test_errors_that_are_all_database_outages_are_infrastructure():
    output = (
        "ERROR tests/test_a.py::test_one - ConnectionRefusedError: [Errno 111] "
        "Connect call failed ('127.0.0.1', 55154)\n"
        "ERROR tests/test_a.py::test_two - asyncpg.exceptions.TooManyConnectionsError: "
        "sorry, too many clients already\n"
        "2 errors in 3.10s\n"
    )
    outcome, reason, failing = classify(1, output)
    assert (outcome, reason) == (INFRASTRUCTURE, "infrastructure_error")
    assert [t["id"] for t in failing] == ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]


def test_one_real_failure_among_outages_is_a_failure():
    output = (
        "ERROR tests/test_a.py::test_one - ConnectionRefusedError: Connect call failed\n"
        "FAILED tests/test_a.py::test_two - AssertionError: assert 1 == 2\n"
        "1 failed, 1 error in 3.10s\n"
    )
    outcome, reason, failing = classify(1, output)
    assert (outcome, reason) == (FAILED, None)
    assert {t["id"] for t in failing} == {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}


def test_a_wrapper_crash_before_any_test_is_infrastructure():
    output = (
        "Traceback (most recent call last):\n"
        '  File "isolated-tests.py", line 23, in provision\n'
        "OSError: Multiple exceptions: [Errno 111] Connect call failed ('127.0.0.1', 55154)\n"
    )
    assert classify(1, output)[:2] == (INFRASTRUCTURE, "infrastructure_error")


def test_a_generic_nonzero_exit_stays_a_failure():
    # A lint or script check says nothing pytest-shaped; its failure is real.
    assert classify(7, "")[:2] == (FAILED, None)


def test_a_collection_error_is_a_failure_the_repair_can_fix():
    output = (
        "ERROR tests/test_a.py - ImportError: cannot import name 'gone' from 'src.a'\n"
        "!!!!!!!! Interrupted: 1 error during collection !!!!!!!!\n"
        "1 error in 0.52s\n"
    )
    outcome, reason, failing = classify(2, output)
    assert (outcome, reason) == (FAILED, None)
    assert failing == [
        {"id": "tests/test_a.py", "reason": "ImportError: cannot import name 'gone' from 'src.a'"}
    ]


def test_only_pytest_node_ids_count_as_failing_tests():
    # Captured output and log lines also start with FAILED/ERROR; an outage
    # printed by a test must not pass for a failing test, nor hide a real one.
    output = (
        "ERROR fetching origin - Connection refused\n"
        "FAILED to reach the daemon\n"
        "ERROR    src.integration.development:development.py:12 boom\n"
        "FAILED tests/test_a.py::TestX::test_x[a-b c] - AssertionError: nope\n"
        "1 failed in 1.00s\n"
    )
    report = parse_pytest_output(output)
    assert report.failing == [
        {"id": "tests/test_a.py::TestX::test_x[a-b c]", "reason": "AssertionError: nope"}
    ]
    assert classify(1, output)[:2] == (FAILED, None)


def test_pytest_summary_parses_decorated_and_quiet_forms():
    decorated = parse_pytest_output("===== 2 failed, 117 passed, 3 skipped in 209.12s (0:03:29) =====")
    assert decorated.summary == {"failed": 2, "passed": 117, "skipped": 3}
    quiet = parse_pytest_output("119 passed in 209.00s")
    assert quiet.summary == {"passed": 119}
    assert parse_pytest_output("1 error in 0.52s").summary == {"errors": 1}
    assert parse_pytest_output("no summary here").summary is None

