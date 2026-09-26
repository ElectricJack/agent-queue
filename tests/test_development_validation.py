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

import pytest

from src.integration.development_result_parser import (
    MAX_FAILING_TESTS,
    MAX_FAILURE_BYTES,
    MAX_JUNIT_BYTES,
    MAX_LINE_BYTES,
    PytestOutputParser,
    classify_report,
    parse_junit,
)
from src.integration.development_validation import (
    FAILED,
    INFRASTRUCTURE,
    PASSED,
    classify,
    parse_pytest_output,
    run_check,
)


async def _run(result, tmp_path, *, timeout=1, slot_wait=5):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    result = {
        "job_id": "job", "input_ref": "candidate", "input_stability": "stable",
        "input_mode": "snapshot", "exit_code": 0, "outcome": PASSED,
        "queue_seconds": 0, "run_seconds": 0.1, **result,
    }
    job = {"id": "job", "result": result, "state": "succeeded"}
    client = SimpleNamespace(submit=AsyncMock(return_value=job), wait=AsyncMock(return_value=job))
    check = await run_check(
        "aq test tests/test_a.py", cwd=tmp_path, timeout_seconds=timeout,
        slot_wait_seconds=slot_wait, job_client=client, project_id="p",
        operation_id="operation", input_ref="candidate", idempotency_key="key",
    )
    client.submit.assert_awaited_once_with(
        project_id="p", operation_id="operation", store=str(tmp_path), input_ref="candidate",
        preset="test", argv=["tests/test_a.py"], idempotency_key="key",
        queue_seconds=slot_wait, run_seconds=timeout,
    )
    return check


async def test_queue_wait_is_not_charged_to_the_run_budget(tmp_path):
    check = await _run({"queue_seconds": 1.5, "run_seconds": 0.3}, tmp_path)
    assert check["outcome"] == PASSED
    assert check["slot_wait_seconds"] == 1.5
    assert check["run_seconds"] == 0.3
    assert check["duration_seconds"] == 1.8


@pytest.mark.parametrize("reason", ["queue_timeout", "run_timeout", "output_store_failed"])
async def test_queue_infrastructure_never_parks_a_batch(tmp_path, reason):
    check = await _run({"outcome": INFRASTRUCTURE, "infra_reason": reason}, tmp_path)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == reason
    assert check["job_id"] == "job"


@pytest.mark.parametrize("outcome", ["lost", "cancelled"])
async def test_lost_and_cancelled_jobs_defer_validation(tmp_path, outcome):
    check = await _run({"outcome": outcome, "exit_code": None}, tmp_path)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == outcome


async def test_real_failure_keeps_result_identity_and_failures_first_excerpt(tmp_path):
    failing = [{"id": "tests/test_a.py::test_batch", "reason": "assert 1 == 2"}]
    check = await _run({
        "outcome": FAILED, "exit_code": 1, "failing_tests": failing,
        "excerpt": "first failure then tail", "result_hash": "immutable-hash",
    }, tmp_path)
    assert check["outcome"] == FAILED
    assert check["failing_tests"] == failing
    assert check["output"] == "first failure then tail"
    assert check["result_hash"] == "immutable-hash"


@pytest.mark.parametrize("change", [
    {"input_ref": "other"}, {"input_stability": "unverified"},
])
async def test_modified_snapshot_never_attests_a_pass(tmp_path, change):
    check = await _run(change, tmp_path)
    assert check["outcome"] == INFRASTRUCTURE
    assert check["infra_reason"] == "snapshot_modified"


async def test_disabled_queue_and_arbitrary_shell_defer_without_a_spawn(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import asyncio

    spawn = AsyncMock(side_effect=AssertionError("adapters must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for command, client, reason in [
        ("aq test tests/a.py", None, "jobs.disabled"),
        ("echo hello", SimpleNamespace(submit=AsyncMock()), "jobs.preset_denied"),
    ]:
        check = await run_check(command, cwd=tmp_path, timeout_seconds=1,
                                slot_wait_seconds=1, job_client=client)
        assert check["outcome"] == INFRASTRUCTURE
        assert check["infra_reason"] == reason
    spawn.assert_not_awaited()


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


def test_checkpoint_cleanup_timeout_is_infrastructure_after_tests_passed():
    output = (
        "119 passed in 209.00s\n"
        "ERROR tests/test_a.py::test_one - RuntimeError: "
        "could not drop leased PostgreSQL test databases: "
        "aq_test_worker: PostgreSQL test database cleanup deadline exceeded\n"
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
    decorated = parse_pytest_output(
        "===== 2 failed, 117 passed, 3 skipped in 209.12s (0:03:29) ====="
    )
    assert decorated.summary == {"failed": 2, "passed": 117, "skipped": 3}
    quiet = parse_pytest_output("119 passed in 209.00s")
    assert quiet.summary == {"passed": 119}
    assert parse_pytest_output("1 error in 0.52s").summary == {"errors": 1}
    assert parse_pytest_output("no summary here").summary is None


# -- executor-independent, bounded result parsing ---------------------------


def test_collection_failure_survives_100_mib_of_passing_noise():
    parser = PytestOutputParser()
    parser.feed(b"ERROR tests/test_a.py - ImportError: missing module\n")
    chunk = b"passing noise\n" * 4096
    for _ in range((100 * 1024 * 1024) // len(chunk) + 1):
        parser.feed(chunk)
    parser.feed(b"1 error in 0.52s\n")
    report = parser.finish()
    assert report.failing == [{"id": "tests/test_a.py", "reason": "ImportError: missing module"}]
    assert report.summary == {"errors": 1}
    assert classify_report(2, report)[:2] == (FAILED, None)
    assert parser.finish() == report


def test_huge_line_invalid_utf8_and_split_codepoints_are_bounded():
    chunks = [
        b"FAILED tests/test_a.py::test_one - ",
        b"x" * (MAX_LINE_BYTES * 20),
        b"\xff\n",
        "FAILED tests/test_b.py::test_two - café\n".encode(),
    ]
    parser = PytestOutputParser()
    for chunk in chunks:
        parser.feed(chunk)
    report = parser.finish()
    split = PytestOutputParser()
    for chunk in chunks:
        for offset in range(0, len(chunk), 997):
            split.feed(chunk[offset : offset + 997])
    assert split.finish() == report
    assert report.truncated
    assert report.failing[1]["reason"] == "café"
    assert all(len(test["reason"].encode()) <= MAX_FAILURE_BYTES for test in report.failing)
    assert len(parser._line.encode()) <= MAX_LINE_BYTES


def test_invalid_utf8_in_failure_is_replaced_and_split_utf8_is_intact():
    parser = PytestOutputParser()
    parser.feed(b"FAILED tests/test_a.py::test_a - \xff caf\xc3")
    parser.feed(b"\xa9\n")
    assert parser.finish().failing[0]["reason"] == "� café"


def test_hundreds_of_failures_are_bounded_sorted_and_reproducible():
    text = "".join(f"FAILED tests/test_a.py::test_{i:03d} - assertion\n" for i in range(350, 0, -1))
    report = parse_pytest_output(text + text.splitlines()[0] + "\n")
    assert len(report.failing) == MAX_FAILING_TESTS
    assert report.omitted_failure_count == 250
    assert report.failing == sorted(report.failing, key=lambda test: test["id"])
    assert report == parse_pytest_output(text + text.splitlines()[0] + "\n")
    assert report.truncated


def test_real_failure_beyond_the_cap_cannot_be_hidden_by_outages():
    text = "".join(f"ERROR tests/test_a.py::test_{i} - Connection refused\n" for i in range(100))
    report = parse_pytest_output(text + "FAILED tests/test_real.py::test_one - assert 1 == 2\n")
    assert report.omitted_failure_count == 1
    assert classify_report(1, report)[:2] == (FAILED, None)


def test_infrastructure_signature_survives_noise_and_chunk_boundaries():
    parser = PytestOutputParser()
    parser.feed(b"Connect call fai")
    parser.feed(b"led\n")
    for _ in range(20):
        parser.feed(b"x" * MAX_LINE_BYTES)
    assert classify_report(1, parser.finish())[:2] == (INFRASTRUCTURE, "infrastructure_error")


def test_an_error_while_parsing_never_creates_a_pass(monkeypatch):
    def broken(_self, _text):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(PytestOutputParser, "_text", broken)
    parser = PytestOutputParser()
    parser.feed(b"100 passed in 1.00s")
    report = parser.finish()
    assert report.parser_error == "text_parser_error"
    assert report.summary is None
    assert classify_report(0, report)[:2] == (INFRASTRUCTURE, "text_parser_error")
    assert classify_report(1, report)[0] == FAILED


@pytest.mark.parametrize(
    ("code", "expected"),
    [(None, "lost"), (5, FAILED), (0, PASSED), (1, FAILED), (137, INFRASTRUCTURE)],
)
def test_managed_result_requires_an_observed_successful_exit(code, expected):
    # Log claims and the validity of XML never replace the observed exit.
    assert classify_report(code, parse_pytest_output("999 passed in 0.10s"))[0] == expected


def test_output_integrity_failure_never_passes():
    report = parse_pytest_output("1 passed in 0.10s")
    assert classify_report(0, report, output_integrity=False)[:2] == (
        INFRASTRUCTURE,
        "output_store_failed",
    )
    assert classify_report(-15, report, cancelled=True)[0] == "cancelled"


def test_junit_is_chunk_independent_with_bounded_failures_and_counts():
    xml = (
        b'<testsuites><testsuite><testcase file="tests/test_a.py" '
        b'classname="tests.test_a.TestA" name="test_a"><failure message="assertion">'
        + b"x"
        * 10000
        + b'</failure></testcase><testcase name="test_b"/>'
        b'<testcase name="test_c"><skipped/></testcase></testsuite></testsuites>'
    )
    whole = parse_junit([xml])
    split = parse_junit(xml[offset : offset + 7] for offset in range(0, len(xml), 7))
    assert whole == split
    assert whole.source == "junit"
    assert whole.artifact_status == "valid"
    assert whole.summary == {"passed": 1, "failed": 1, "skipped": 1}
    assert whole.failing[0]["id"] == "tests/test_a.py::TestA::test_a"
    assert len(whole.failing[0]["reason"].encode()) <= MAX_FAILURE_BYTES
    assert classify_report(1, whole)[0] == FAILED


def test_junit_hundreds_of_failures_count_omissions_and_classify_all():
    cases = "".join(
        f'<testcase name="test_{i}"><error message="connection refused"/></testcase>'
        for i in range(120)
    )
    xml = (
        "<testsuite>" + cases + '<testcase name="test_real"><failure message="assert"/>'
        "</testcase></testsuite>"
    ).encode()
    report = parse_junit([xml])
    assert len(report.failing) == 100
    assert report.omitted_failure_count == 21
    assert report.summary == {"errors": 120, "failed": 1}
    assert classify_report(1, report)[0] == FAILED


@pytest.mark.parametrize(
    ("chunks", "error"),
    [
        (None, "missing_junit"),
        ([b"<testsuite>"], "malformed_junit"),
        ([b"<unrelated/>"], "malformed_junit"),
        (
            [b'<!DOCTYPE testsuite [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuite/>'],
            "unsafe_junit",
        ),
        ([b'<!DOCTYPE testsuite [<!ENTITY x "expanded">]><testsuite/>'], "unsafe_junit"),
    ],
)
def test_missing_malformed_and_entity_junit_never_creates_a_pass(chunks, error):
    report = parse_junit(chunks)
    assert report.artifact_status == error
    assert report.summary is None
    assert classify_report(0, report)[:2] == (INFRASTRUCTURE, error)
    assert classify_report(1, report)[0] == FAILED
    assert classify_report(5, report)[0] == FAILED
    assert classify_report(None, report)[0] == "lost"


def test_junit_artifact_byte_limit_is_explicit():
    report = parse_junit([b"<testsuite>", b" " * MAX_JUNIT_BYTES])
    assert report.parser_error == "junit_limit"
    assert classify_report(0, report)[:2] == (INFRASTRUCTURE, "junit_limit")


def test_streaming_parser_keeps_early_failure_after_large_output():
    parser = PytestOutputParser()
    parser.feed(b"ERROR tests/test_a.py - ImportError: missing module\n")
    for _ in range(100):
        parser.feed(b"passing noise\n" * 400)
    assert parser.finish().failing == [
        {"id": "tests/test_a.py", "reason": "ImportError: missing module"}
    ]


def test_outage_words_in_a_node_id_cannot_hide_a_real_assertion():
    report = parse_pytest_output(
        "FAILED tests/test_a.py::test_a[connection refused] - AssertionError: nope\n"
    )
    assert classify_report(1, report)[:2] == (FAILED, None)


def test_streaming_parser_error_cannot_pass(monkeypatch):
    def broken(_self, _text):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(PytestOutputParser, "_text", broken)
    parser = PytestOutputParser()
    parser.feed(b"successful\n")
    assert classify_report(0, parser.finish())[:2] == (INFRASTRUCTURE, "text_parser_error")


def test_junit_empty_suite_is_not_validation_and_kills_keep_signal_outcomes():
    report = parse_junit([b"<testsuite/>"])
    assert classify_report(0, report)[:2] == (INFRASTRUCTURE, "no_tests_collected")
    assert classify_report(-9, report)[:2] == (INFRASTRUCTURE, "killed")
