"""Recorded red-run normalization, build precedence and frozen-diff attribution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import random

import pytest

from src.integration.failure_groups import (
    FrozenMemberFiles,
    group_failure_records,
    group_run_failures,
    normalize_failure_records,
    normalize_signature,
)
from src.integration.models import RequiredCheckSet


@pytest.fixture(autouse=True)
def _pg_backend():
    """This boundary is pure: no test database is needed."""


@pytest.fixture
def recorded():
    return json.loads((Path(__file__).parent / "fixtures/integration/failure-groups.json").read_text())


def group(recorded):
    return group_run_failures(
        recorded["checks"], head=recorded["head"],
        required=RequiredCheckSet(version="v1", producer_id="15368",
                                  names=("linux", "windows", "lint")),
        members=[FrozenMemberFiles(**member) for member in recorded["members"]],
        job_logs=recorded["job_logs"],
    )


def check(**changes):
    return {"id": 1, "name": "test", "head_sha": "head", "app": {"id": 15368},
            "status": "completed", "conclusion": "failure", **changes}


def run(checks, **kwargs):
    return group_run_failures(checks, head="head", required=RequiredCheckSet(
        version="v1", producer_id="15368", names=("test",)), members=(), **kwargs)


def test_recorded_groups_are_stable_without_mutating_inputs(recorded):
    original = deepcopy(recorded)
    expected = asdict(group(recorded))
    assert recorded == original
    rng = random.Random(42)
    for _ in range(20):
        shuffled = deepcopy(recorded)
        rng.shuffle(shuffled["checks"])
        rng.shuffle(shuffled["job_logs"])
        rng.shuffle(shuffled["members"])
        for member in shuffled["members"]:
            rng.shuffle(member["files"])
        for log in shuffled["job_logs"]:
            rng.shuffle(log["records"])
        assert asdict(group(shuffled)) == expected


def test_build_noise_is_discarded_only_on_its_job_and_platform(recorded):
    groups = group(recorded).groups
    assert groups[0].kind == "build"
    assert groups[0].attributed_member == "native"
    assert groups[0].count == 1
    assert all(record.kind != "test" or record.platform != "linux"
               for result in groups for record in result.records)
    assert any(result.kind == "test" and result.platform == "windows" for result in groups)
    assert [(result.kind != "build", -result.count, result.key) for result in groups] == sorted(
        (result.kind != "build", -result.count, result.key) for result in groups)


def test_symptoms_merge_and_keep_exact_frozen_proof(recorded):
    api = next(result for result in group(recorded).groups if result.attributed_member == "api")
    assert api.count == 2
    assert api.subsystem == "tests/api"
    assert len(api.signatures) == 2
    assert api.implicated_members == ("api",)
    assert api.attribution == "attributed"
    assert dict(api.file_owners)["src/api/service.py"] == ("api",)
    assert next(member for member in api.frozen_members if member.task_id == "api").source_sha == "S-api"
    assert any(result.attributed_member == "db" for result in group(recorded).groups)


def test_shared_files_and_untouched_files_never_blame_a_member(recorded):
    groups = group(recorded).groups
    overlap = next(result for result in groups if "src/shared.py" in dict(result.file_owners))
    untouched = next(result for result in groups if "src/preexisting.py" in dict(result.file_owners))
    assert overlap.attribution == untouched.attribution == "shared"
    assert dict(overlap.file_owners)["src/shared.py"] == ("api", "native")
    assert dict(untouched.file_owners)["src/preexisting.py"] == ()


@pytest.mark.parametrize("files", [(), ("tests/unit/a.py",),
                                   ("tests/unit/a.py", "src/untouched.py"),
                                   ("tests/unit/a.py", "src/other.py")])
def test_attribution_requires_every_file_exclusively_owned_by_same_member(files):
    members = (FrozenMemberFiles("a", "E", "Sa", ("tests/unit/a.py",)),
               FrozenMemberFiles("b", "E", "Sb", ("src/other.py",)))
    records = normalize_failure_records(check(output={"annotations": [
        {"kind": "test", "message": "AssertionError: mismatch", "files": files}]}))
    result, = group_failure_records(records, members)
    assert result.attributed_member == ("a" if files == ("tests/unit/a.py",) else None)


@pytest.mark.parametrize("conclusion,state", [("cancelled", "cancelled"),
                                             ("success", "green"), (None, "pending")])
def test_non_red_runs_have_no_groups_even_with_failure_logs(conclusion, state):
    result = run([check(conclusion=conclusion, status="in_progress" if conclusion is None else
                        "completed", output={"summary": "error: failed"})],
                 job_logs=[{"check_id": 1, "text": "error: cannot compile"}])
    assert result.state == state
    assert not result.groups
    assert result.evidence["infra_only"] == (state == "cancelled")


def test_cancelled_only_workflow_failure_is_infrastructure(recorded):
    for entry in recorded["checks"]:
        entry.update(conclusion="cancelled", workflow_run_conclusion="failure")
    result = group(recorded)
    assert result.state == "cancelled"
    assert result.evidence["infra_only"]
    assert result.groups == ()


def test_latest_trusted_exact_head_required_check_controls_log_selection():
    stale = check(id=1, started_at="2026-01-01")
    fresh = check(id=2, started_at="2026-01-02", output={"summary": "F401 unused import"})
    ignored = [check(id=3, head_sha="other"), check(id=4, app={"id": 9}),
               check(id=5, name="optional"), stale]
    result = run([fresh, *ignored], job_logs=[{"check_id": entry["id"], "text": "error: stale"}
                                            for entry in ignored])
    assert result.state == "red"
    assert len(result.groups) == 1
    assert {record.check_id for group in result.groups for record in group.records} == {"2"}
    assert result.groups[0].kind == "other"
    assert run([stale, {**fresh, "conclusion": "success"}]).state == "green"


def test_genuine_failure_alongside_cancellation_remains_a_verdict(recorded):
    recorded["checks"][1]["conclusion"] = "cancelled"
    assert group(recorded).state == "red"
    assert all(result.platform != "windows" for result in group(recorded).groups)


def test_signature_removes_volatile_addresses_temporary_paths_and_numbers():
    assert normalize_signature("AssertionError: 12 at 0xdead in /tmp/pytest-12/a") == (
        normalize_signature("AssertionError: 99 at 0xbeef in /tmp/pytest-99/b"))
    assert normalize_signature("note\nValueError: 42\nTypeError: later") == "ValueError: <n>"
    assert normalize_signature(r"error: read C:\Users\runner\Temp\build-123\file at 0xff") == (
        "error: read <tmp> at <address>")


def test_raw_pytest_log_yields_failing_tests_and_ignores_passes():
    logs = [{"check_id": 1, "text": "tests/api/test_a.py::test_ok PASSED\n"
             "FAILED tests/api/test_a.py::test_one - AssertionError: 1\n"
             "FAILED tests/api/test_b.py::test_two - TypeError: bad value\n"}]
    result = run([check()], job_logs=logs)
    assert result.groups[0].count == 2
    assert result.groups[0].subsystem == "tests/api"
    assert {file for record in result.groups[0].records for file in record.files} == {
        "tests/api/test_a.py", "tests/api/test_b.py"}


def test_raw_mixed_build_and_test_log_preserves_build_precedence():
    result = run([check()], job_logs=[{"check_id": 1, "text":
        "2026-10-21T12:00:00.123Z src/native/core.cpp:12: error: missing symbol\n"
        "2026-10-21T12:00:00.124Z FAILED tests/api/test_a.py::test_one - AssertionError: 1"}])
    assert len(result.groups) == 1
    assert result.groups[0].kind == "build"
    assert result.groups[0].file_owners == (("src/native/core.cpp", ()),)


def test_unrecognized_log_never_attributes_passed_test_files():
    records = normalize_failure_records(check(), [{"text": "tests/api/test_a.py PASSED"}])
    assert records[0].files == ()
    assert records[0].message == "test: failure"


def test_lint_error_does_not_suppress_tests_on_same_job():
    records = normalize_failure_records(check(job="mypy"), [{"text":
        "src/api/routes.py:42: error: Incompatible types"}])
    assert records[0].kind == "other"
    tests = normalize_failure_records(check(job="mypy", output={"annotations": [
        {"kind": "test", "path": "tests/api/test_routes.py", "message": "AssertionError: mismatch"}
    ]}))
    assert {result.kind for result in group_failure_records((*records, *tests), ())} == {
        "other", "test"}


def test_build_does_not_suppress_other_jobs_on_same_platform():
    records = normalize_failure_records(check(platform="linux", output={"annotations": [
        {"job": "compile", "kind": "build", "message": "error: cannot compile"},
        {"job": "unit", "kind": "test", "message": "AssertionError: mismatch"},
    ]}))
    assert {result.job for result in group_failure_records(records, ())} == {"compile", "unit"}


def test_traceback_uses_repository_frames_and_first_error_without_filesystem_io():
    records = normalize_failure_records(check(), [{"text": 'Traceback:\n'
        '  File "/repo/src/api/service.py", line 42\n'
        '  File "/usr/lib/python3.12/site.py", line 99\n'
        'ValueError: expected 3\nTypeError: later error'}], repository_root="/repo")
    assert len(records) == 1
    assert records[0].files == ("src/api/service.py",)
    assert records[0].signature == "ValueError: expected <n>"
    assert records[0].subsystem == "src/api"


def test_builds_with_distinct_first_errors_stay_separate_and_duplicate_evidence_deduplicates():
    entry = check(job="build", output={"annotations": [
        {"message": "error: missing symbol"}, {"message": "error: bad include"},
        {"message": "error: missing symbol"},
    ]})
    result = run([entry])
    assert len(result.groups) == 2
    assert all(result.kind == "build" and result.count == 1 for result in result.groups)


def test_ignored_warning_does_not_mask_an_unknown_failed_check():
    result = run([check(output={"annotations": [{"annotation_level": "warning",
                                                 "message": "error: advisory"}]})])
    assert result.groups[0].attribution == "shared"
    assert result.groups[0].records[0].message == "test: failure"


def test_changed_signatures_change_identity_but_input_order_does_not(recorded):
    before = group(recorded).groups[0].key
    recorded["checks"][0]["output"]["annotations"][0]["message"] = "error: different cause"
    assert group(recorded).groups[0].key != before


def test_duplicate_member_evidence_is_rejected():
    member = FrozenMemberFiles("task", "E", "S", ())
    with pytest.raises(ValueError, match="duplicate frozen member"):
        group_failure_records((), (member, member))
