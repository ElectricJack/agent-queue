"""Tests for the deterministic deliverable evaluator (``src/deliverables.py``).

The evaluator must accept the shapes planners actually write: a ``test``
target may be a single module path *or* the shell command that runs one or
more suites, and a ``command`` target may be a shell command whose evidence
is a recorded ``--command`` value rather than text found in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.task_commands import normalize_deliverables
from src.deliverables import evaluate_deliverables


def _one(kind: str, target: str):
    return normalize_deliverables([{"id": "item", "kind": kind, "target": target}])


class TestDeclarationRejectsUnsatisfiablePathTargets:
    def test_file_target_that_contains_whitespace_is_rejected(self):
        items, error = _one("file", "src/a.py src/b.py")
        assert items == []
        assert error and "one repo-relative file path" in error

    def test_absolute_target_is_rejected(self):
        items, error = _one("file", "/etc/passwd")
        assert items == []
        assert error and "repo-relative" in error

    def test_parent_escape_is_rejected(self):
        items, error = _one("test", "../other-repo/tests/test_x.py")
        assert items == []
        assert error and "repo-relative" in error

    def test_pytest_node_id_is_rejected(self):
        items, error = _one("test", "tests/test_x.py::test_case")
        assert items == []
        assert error and "one repo-relative file path" in error


class TestDeclarationKeepsSatisfiableItems:
    @pytest.mark.parametrize("kind", ["file", "test"])
    def test_single_relative_path_is_stored(self, kind):
        items, error = _one(kind, "tests/test_x.py")
        assert error is None
        assert items == [{"id": "item", "kind": kind, "target": "tests/test_x.py"}]

    def test_grep_kinds_still_accept_command_shaped_targets(self):
        items, error = _one("command", "aq task close --deliverable-unmet")
        assert error is None
        assert items[0]["target"] == "aq task close --deliverable-unmet"

    def test_test_command_is_stored_for_close_time_evidence(self):
        command = "aq test tests/test_a.py tests/test_b.py"
        items, error = _one("test", command)
        assert error is None
        assert items[0]["target"] == command


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "tests" / "test_b.py").write_text("def test_b(): pass\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.py").write_text("register('graph_tidy')\n")
    return tmp_path


def _met(deliverable, *, root, tests=(), commands=()):
    [result] = evaluate_deliverables(
        [deliverable], root=root, tests=list(tests), commands=list(commands)
    )
    return result["met"]


# --- kind=test, single module path (existing contract) ---------------------


def test_test_module_path_needs_the_file_and_a_recorded_test_command(repo):
    item = {"id": "suite", "kind": "test", "target": "tests/test_a.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_a.py"]) is True
    assert _met(item, root=repo, tests=[]) is False
    assert _met(item, root=repo, commands=["aq test tests/test_a.py"]) is False


def test_test_module_path_that_does_not_exist_is_unmet(repo):
    item = {"id": "suite", "kind": "test", "target": "tests/test_missing.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_missing.py"]) is False


# --- kind=test, shell command target ---------------------------------------


def test_test_command_target_is_met_by_the_identical_recorded_test(repo):
    item = {
        "id": "focused-suite",
        "kind": "test",
        "target": "aq test tests/test_a.py tests/test_b.py",
    }
    assert _met(item, root=repo, tests=["aq test tests/test_a.py tests/test_b.py"]) is True


def test_test_command_target_ignores_whitespace_differences(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test  tests/test_a.py   tests/test_b.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_a.py tests/test_b.py"]) is True


def test_test_command_target_is_met_when_every_suite_was_run_separately(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test tests/test_a.py tests/test_b.py"}
    tests = ["aq test tests/test_a.py -x", "aq test tests/test_b.py"]
    assert _met(item, root=repo, tests=tests) is True


def test_test_command_target_is_unmet_when_a_suite_was_skipped(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test tests/test_a.py tests/test_b.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_a.py"]) is False


def test_test_command_target_is_unmet_when_a_named_suite_does_not_exist(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test tests/test_missing.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_missing.py"]) is False


def test_test_command_target_is_not_satisfied_by_a_command_record(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test tests/test_a.py"}
    assert _met(item, root=repo, commands=["aq test tests/test_a.py"]) is False


def test_test_command_target_without_paths_needs_a_matching_recorded_test(repo):
    item = {"id": "suite", "kind": "test", "target": "npm test"}
    assert _met(item, root=repo, tests=["npm test"]) is True
    assert _met(item, root=repo, tests=["npm run lint"]) is False


# --- kind=command ----------------------------------------------------------


def test_command_target_is_met_by_a_recorded_command(repo):
    item = {"id": "ruff", "kind": "command", "target": "ruff check src/cli.py"}
    assert _met(item, root=repo, commands=["ruff check src/cli.py"]) is True
    assert _met(item, root=repo, commands=[]) is False


def test_command_target_placeholder_matches_any_arguments(repo):
    item = {"id": "ruff", "kind": "command", "target": "ruff check <changed files>"}
    assert _met(item, root=repo, commands=["ruff check src/cli.py tests/test_a.py"]) is True
    assert _met(item, root=repo, commands=["ruff format src/cli.py"]) is False


def test_command_target_is_also_met_by_a_recorded_test_command(repo):
    item = {"id": "suite", "kind": "command", "target": "aq test tests/test_a.py"}
    assert _met(item, root=repo, tests=["aq test tests/test_a.py"]) is True


def test_multi_word_command_target_is_not_met_by_repo_text(repo):
    (repo / "docs.md").write_text("Run `ruff check <changed files>` before closing.\n")
    item = {"id": "ruff", "kind": "command", "target": "ruff check <changed files>"}
    assert _met(item, root=repo) is False


def test_single_word_command_target_keeps_the_repo_text_check(repo):
    item = {"id": "tidy", "kind": "command", "target": "graph_tidy"}
    assert _met(item, root=repo) is True
    assert _met({**item, "target": "graph_untidy"}, root=repo) is False


# --- other kinds are unchanged ---------------------------------------------


def test_flag_and_registration_targets_use_the_repo_text_check(repo):
    assert _met({"id": "f", "kind": "flag", "target": "graph_tidy"}, root=repo) is True
    assert _met({"id": "r", "kind": "registration", "target": "nope"}, root=repo) is False


def test_evaluate_keeps_declaration_fields_and_adds_met_and_reason(repo):
    results = evaluate_deliverables(
        [{"id": "cli", "kind": "file", "target": "src/cli.py"}], root=repo, tests=[], commands=[]
    )
    assert results == [
        {"id": "cli", "kind": "file", "target": "src/cli.py", "met": True, "reason": ""}
    ]


def test_commands_defaults_to_empty(repo):
    item = {"id": "ruff", "kind": "command", "target": "ruff check src/cli.py"}
    [result] = evaluate_deliverables([item], root=repo, tests=[])
    assert result["met"] is False


def test_test_command_target_accepts_directories_and_node_ids(repo):
    item = {"id": "suite", "kind": "test", "target": "aq test tests/ -k 'a or b'"}
    assert _met(item, root=repo, tests=["aq test tests/ -k 'a or b'"]) is True
    item = {"id": "one", "kind": "test", "target": "pytest tests/test_a.py::test_a"}
    assert _met(item, root=repo, tests=["pytest tests/test_a.py::test_a"]) is True
    item = {"id": "gone", "kind": "test", "target": "pytest tests/test_gone.py::test_a"}
    assert _met(item, root=repo, tests=["pytest tests/test_gone.py::test_a"]) is False
