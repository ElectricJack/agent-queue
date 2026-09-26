"""Exact membership and immutable provenance without a second pytest parser."""

from src.resources.test_baseline import capture_latest_baseline, read_baseline

COMMIT = "a" * 40


def _note(tmp_path, name="full-suite-baseline-2026-09-24.md", failures=None):
    failures = failures if failures is not None else ["tests/test_a.py::test_it[one two]"]
    path = tmp_path / name
    path.write_text(
        f"Recorded main SHA `{COMMIT}`.\n\n## Known failures on main\n\n"
        + "\n".join(f"- `{f}`" for f in failures)
    )
    return path


def test_parameter_values_preserved_and_failure_outcome_unchanged(tmp_path):
    path = _note(tmp_path)
    snapshot = read_baseline(path, captured_at=20, comparison_commit=COMMIT)
    original = {"exit_code": 1, "outcome": "failed"}
    result = {
        **original,
        **snapshot.annotate(
            [
                " tests/test_a.py::test_it[one two] ",
                "tests/test_a.py::test_it[one]",
            ],
            parse_complete=True,
        ),
    }
    assert result["exit_code"] == 1 and result["outcome"] == "failed"
    assert result["known_failures"] == ["tests/test_a.py::test_it[one two]"]
    assert result["not_in_baseline"] == ["tests/test_a.py::test_it[one]"]
    assert (
        result["comparison_complete"]
        and "does not establish causation" in result["comparison_note"]
    )
    assert result["baseline_ref"] == str(path)
    assert result["baseline_captured_at"] == 20 and result["baseline_content_hash"].startswith(
        "sha256:"
    )


def test_changed_file_does_not_reclassify_captured_snapshot(tmp_path):
    path = _note(tmp_path)
    before = read_baseline(path, captured_at=10, comparison_commit=COMMIT)
    _note(tmp_path, failures=["tests/test_b.py::test_new"])
    after = read_baseline(path, captured_at=20, comparison_commit=COMMIT)
    assert before.content_hash != after.content_hash
    assert before.annotate(["tests/test_a.py::test_it[one two]"], parse_complete=True)[
        "known_failures"
    ]
    assert not after.annotate(["tests/test_a.py::test_it[one two]"], parse_complete=True)[
        "known_failures"
    ]


def test_missing_invalid_and_stale_are_unavailable(tmp_path):
    missing = read_baseline(tmp_path / "absent", captured_at=1, comparison_commit=COMMIT)
    path = _note(tmp_path)
    stale = read_baseline(path, captured_at=1, comparison_commit="b" * 40)
    path.write_text("unstructured report without identity")
    invalid = read_baseline(path, captured_at=1, comparison_commit=COMMIT)
    assert [s.status for s in (missing, invalid, stale)] == ["missing", "invalid", "stale"]
    for snapshot in (missing, invalid, stale):
        result = snapshot.annotate(["tests/test_a.py::test_it"], parse_complete=True)
        assert not result["comparison_complete"]
        assert result["known_failures"] == result["not_in_baseline"] == []
        assert result["comparison_note"] == "Comparison unavailable."
        assert result["failure_ids"] == ["tests/test_a.py::test_it"]


def test_incomplete_parse_is_never_complete_comparison(tmp_path):
    snapshot = read_baseline(_note(tmp_path), captured_at=1, comparison_commit=COMMIT)
    result = snapshot.annotate(["tests/test_a.py::test_it[one two]"], parse_complete=False)
    assert result["known_failures"] and not result["comparison_complete"]
    assert "incomplete" in result["comparison_note"]


def test_latest_valid_baseline_skips_invalid_newer_note(tmp_path):
    valid = _note(tmp_path, "full-suite-baseline-2026-09-22.md")
    invalid = _note(tmp_path)
    invalid.write_text("not a recorded baseline")
    snapshot = capture_latest_baseline(tmp_path, captured_at=1, comparison_commit=COMMIT)
    assert snapshot.status == "matched" and snapshot.baseline_ref == str(valid)
    valid.write_text(f"main SHA `{COMMIT}`\n## Known failures\n- `tests/a.py::t`\n- unparseable")
    assert read_baseline(valid, captured_at=1, comparison_commit=COMMIT).status == "invalid"
