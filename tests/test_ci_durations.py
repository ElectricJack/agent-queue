"""Refreshing timings must not replace a full map with partial shard data."""

import json
import sys

import pytest

from scripts.merge_test_durations import main, merge_durations


def write_shard(directory, group, durations):
    path = directory / f"artifact-{group}" / f"default-{group}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(durations))
    return path


def test_merge_all_downloaded_artifacts(tmp_path):
    write_shard(tmp_path, 1, {"tests/test_a.py::test_a": 0.5})
    write_shard(tmp_path, 2, {"tests/test_b.py::test_b": 2})
    assert merge_durations(tmp_path, 2) == {
        "tests/test_a.py::test_a": 0.5,
        "tests/test_b.py::test_b": 2,
    }


def test_missing_shard_preserves_existing_output(tmp_path, monkeypatch):
    write_shard(tmp_path, 1, {"tests/test_a.py::test_a": 0.5})
    output = tmp_path / "timings.json"
    output.write_text("original")
    monkeypatch.setattr(sys, "argv", ["merge", str(tmp_path), "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert output.read_text() == "original"


def test_refresh_writes_complete_map(tmp_path, monkeypatch):
    for group in range(1, 9):
        write_shard(tmp_path, group, {f"tests/test_{group}.py::test_a": group})
    output = tmp_path / "timings.json"
    monkeypatch.setattr(sys, "argv", ["merge", str(tmp_path), "--output", str(output)])
    main()
    assert json.loads(output.read_text()) == {
        f"tests/test_{group}.py::test_a": group for group in range(1, 9)
    }


def test_duplicate_artifact_is_rejected(tmp_path):
    path = write_shard(tmp_path, 1, {"tests/test_a.py::test_a": 0.5})
    duplicate = tmp_path / path.name
    duplicate.write_text(path.read_text())
    with pytest.raises(ValueError, match="exactly one artifact"):
        merge_durations(tmp_path, 1)


def test_overlapping_tests_are_rejected(tmp_path):
    for group in (1, 2):
        write_shard(tmp_path, group, {"tests/test_a.py::test_a": 0.5})
    with pytest.raises(ValueError, match="duplicate test ID"):
        merge_durations(tmp_path, 2)


@pytest.mark.parametrize(
    "durations",
    [
        {},
        [],
        {"invalid": 1},
        {"tests/test_a.py::test_a": -1},
        {"tests/test_a.py::test_a": True},
        {"tests/test_a.py::test_a": "1"},
        {"tests/test_a.py::test_a": float("nan")},
        {"tests/test_a.py::test_a": float("inf")},
    ],
)
def test_invalid_timings_are_rejected(tmp_path, durations):
    write_shard(tmp_path, 1, durations)
    with pytest.raises(ValueError):
        merge_durations(tmp_path, 1)
