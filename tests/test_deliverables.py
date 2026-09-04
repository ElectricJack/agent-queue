"""Declaration-time validation and close-time evaluation of task deliverables.

A ``file`` or ``test`` deliverable is checked at close by resolving its target
as a repo-relative path, so a target that is not a single path (a whole
``aq test ...`` command, an absolute path, a ``..`` escape, a ``::`` node id)
can never evaluate true.  ``normalize_deliverables`` must refuse to store such
an item instead of letting the close-time check surface it as a false gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.task_commands import normalize_deliverables
from src.deliverables import evaluate_deliverables

_COMMAND = (
    "aq test tests/test_reviewer_api_scope.py tests/test_orchestrator.py tests/test_merge_slot.py"
)


def _one(kind: str, target: str):
    return normalize_deliverables([{"id": "item", "kind": kind, "target": target}])


class TestDeclarationRejectsUnsatisfiablePathTargets:
    def test_test_target_that_is_a_command_is_rejected_with_path_shape_hint(self):
        items, error = _one("test", _COMMAND)

        assert items == []
        assert error is not None
        assert "deliverables[0].target" in error
        assert "one repo-relative file path" in error
        assert "one item per path" in error

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


class TestEvaluation:
    def test_test_item_is_met_when_its_path_exists_and_a_recorded_command_names_it(
        self, tmp_path: Path
    ):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        results = evaluate_deliverables(
            [{"id": "t", "kind": "test", "target": "tests/test_x.py"}],
            root=tmp_path,
            tests=["aq test tests/test_x.py tests/test_y.py"],
        )
        assert [r["met"] for r in results] == [True]

    def test_test_item_is_unmet_when_no_recorded_command_names_it(self, tmp_path: Path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        results = evaluate_deliverables(
            [{"id": "t", "kind": "test", "target": "tests/test_x.py"}],
            root=tmp_path,
            tests=["aq test tests/test_y.py"],
        )
        assert [r["met"] for r in results] == [False]
