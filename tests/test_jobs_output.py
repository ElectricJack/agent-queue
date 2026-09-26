"""Logical output retention, crash fencing and deterministic result v1."""

import hashlib
import json
import os
import uuid

import pytest
from src.jobs.artifacts import OutputStore, atomic_json, job_directory, read_json
from src.jobs.result import build_result
from src.integration.development_result_parser import PytestOutputParser
from src.jobs.result import report_json


def test_store_retains_head_and_tail_with_explicit_gap(tmp_path):
    store = OutputStore(tmp_path, head_bytes=8, tail_bytes=16)
    store.append(b"HEADabcd")
    store.append(b"x" * 16)
    store.append(b"LASTefghijklmnop")
    result = store.read(0, 100)
    assert result["chunks"] == [
        {"offset": 0, "data": b"HEADabcd"},
        {"offset": 24, "data": b"LASTefghijklmnop"},
    ]
    assert result["gaps"] == [{"after": 8, "next": 24}]
    assert result["next"] == 40
    assert store.stats() == {
        "output_bytes_seen": 40,
        "output_bytes_retained": 24,
        "truncated": True,
    }
    store.close()
    reopened = OutputStore(tmp_path, head_bytes=8, tail_bytes=16)
    assert reopened.read(9, 4)["gaps"] == [{"after": 9, "next": 24}]
    assert reopened.read(9, 4)["chunks"][0]["data"] == b"LAST"
    reopened.close()


def test_crash_during_tail_overwrite_never_returns_wrong_offset(tmp_path):
    store = OutputStore(tmp_path, head_bytes=1, tail_bytes=16)
    store.append(b"H")
    store.append(b"a" * 16)
    os.pwrite(store._tail, b"b" * 16, 0)  # overwrite preceded manifest commit
    store.close()
    reopened = OutputStore(tmp_path, head_bytes=1, tail_bytes=16)
    result = reopened.read(1, 100)
    assert result["chunks"] == []
    assert result["gaps"] == [{"after": 1, "next": 17}]
    assert reopened.failed
    reopened.close()


def test_huge_line_cap_and_byte_cursor(tmp_path):
    store = OutputStore(tmp_path, head_bytes=16, tail_bytes=32 * 1024)
    store.append(b"\xff" + "🐈".encode() * 100000)
    assert store.stats()["output_bytes_retained"] <= 16 + 32 * 1024
    assert (tmp_path / "output.tail").stat().st_size <= 32 * 1024
    assert store.read(0, 1)["chunks"][0]["data"] == b"\xff"
    with pytest.raises(ValueError):
        store.read(-1)
    store.close()


def test_symlink_artifacts_and_ids_refused(tmp_path):
    with pytest.raises(ValueError):
        job_directory(tmp_path, "../../foreign")
    directory = job_directory(tmp_path, str(uuid.uuid4()))
    target = tmp_path / "secret"
    target.write_text("secret")
    (directory / "output.head").symlink_to(target)
    with pytest.raises(OSError):
        OutputStore(directory)
    assert target.read_text() == "secret"


@pytest.mark.parametrize(
    "receipt,outcome,reason",
    [
        ({"exit_code": 0}, "passed", None),
        ({"exit_code": 5}, "failed", "no_tests_collected"),
        ({"exit_code": 0, "output_store_failed": True}, "infrastructure", "output_store_failed"),
        ({"exit_code": 0, "infra_reason": "run_timeout"}, "infrastructure", "run_timeout"),
        ({"cancelled": True}, "cancelled", None),
        (None, "lost", "missing_receipt"),
    ],
)
def test_result_exit_integrity_and_missing_receipt(receipt, outcome, reason):
    job = {"id": str(uuid.uuid4()), "preset": "test", "input_mode": "live"}
    result = build_result(job, receipt)
    assert (result["outcome"], result["infra_reason"]) == (outcome, reason)
    assert result["input_stability"] == "unverified"
    unhashed = {k: v for k, v in result.items() if k != "result_hash"}
    assert (
        result["result_hash"]
        == hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_failures_first_valid_utf8_bounded_and_deterministic():
    parser = PytestOutputParser()
    for i in range(200):
        parser.feed(f"FAILED tests/a.py::test_{i:03d} - {'🐈' * 1000}\n".encode())
    receipt = {"exit_code": 1, "report": report_json(parser.finish())}
    job = {"id": str(uuid.uuid4()), "preset": "test", "input_mode": "snapshot"}
    result = build_result(job, receipt, b"\x1b[31m" + b"noise" * 4000 + b"\xff")
    assert len(result["excerpt"].encode()) <= 8192
    assert "tests/a.py::test_000" in result["excerpt"]
    assert "tests/a.py::test_001" in result["excerpt"]
    assert "\x1b" not in result["excerpt"]
    assert result["omitted_failure_count"] == 100
    assert result == build_result(job, receipt, b"\x1b[31m" + b"noise" * 4000 + b"\xff")


def test_atomic_receipt_roundtrip(tmp_path):
    path = tmp_path / "completion.json"
    atomic_json(path, {"exit_code": 0})
    assert read_json(path) == {"exit_code": 0}
    assert not list(tmp_path.glob(".*.tmp"))
