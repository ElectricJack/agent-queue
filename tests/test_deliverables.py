"""Tests for the deterministic deliverable evaluator (``src/deliverables.py``).

The evaluator must accept the shapes planners actually write: a ``test``
target may be a single module path *or* the shell command that runs one or
more suites, and a ``command`` target may be a shell command whose evidence
is a recorded ``--command`` value rather than text found in the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.commands.task_commands import normalize_deliverables
from src.deliverables import effective_deliverables, evaluate_deliverables
from src.models import TaskType


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


_DASHBOARD_TEST_PATHS = [
    f"src/pages/playbook-graph-v2/__tests__/{name}"
    for name in (
        "types.test.ts",
        "StepNodeCard.test.tsx",
        "SemanticNodeInspector.test.tsx",
        "AdvancedNodeDetail.test.tsx",
        "RunOverlayPanel.test.tsx",
    )
]


@pytest.fixture
def npm_repo(repo):
    (repo / "package.json").write_text(json.dumps({"workspaces": ["dashboard"]}))
    (repo / "dashboard").mkdir()
    (repo / "dashboard/package.json").write_text(json.dumps({"name": "@queue/dashboard"}))
    for path in _DASHBOARD_TEST_PATHS:
        file = repo / "dashboard" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("// regression fixture\n")
    return repo


def _npm_suite(prefix="npm -w dashboard run test"):
    return prefix + " -- " + " ".join(_DASHBOARD_TEST_PATHS)


@pytest.mark.parametrize(
    "prefix",
    [
        "npm -w dashboard run test",
        "npm --workspace dashboard run test",
        "npm --workspace=dashboard run test",
        "npm -wdashboard run test",
        "npm -w './dashboard/' run test",
        "npm run test -w dashboard",
        "npm run test --workspace=dashboard",
        "npm test --workspace dashboard",
        "npm --workspace=@queue/dashboard run-script test",
    ],
)
def test_npm_workspace_resolves_the_exact_five_dashboard_paths(npm_repo, prefix):
    """B1 paths exist only below dashboard, never at the worktree root."""
    command = _npm_suite(prefix)
    item = {"id": "dashboard", "kind": "test", "target": command}
    assert all(not (npm_repo / path).exists() for path in _DASHBOARD_TEST_PATHS)
    assert _met(item, root=npm_repo, tests=[command]) is True
    assert _met(item, root=npm_repo, commands=[command]) is False


def test_npm_workspace_needs_exact_test_evidence_from_the_same_context(npm_repo):
    command = _npm_suite()
    item = {"id": "dashboard", "kind": "test", "target": command}
    assert _met(item, root=npm_repo, tests=[command.replace(" ", "  ")]) is True
    for evidence in (
        [],
        [_npm_suite("npm run test")],
        [_npm_suite("npm -w other run test")],
        [f"aq test {path}" for path in _DASHBOARD_TEST_PATHS],
        ["cd other && " + command],
        [command + " --exclude " + _DASHBOARD_TEST_PATHS[0]],
    ):
        assert _met(item, root=npm_repo, tests=evidence) is False


def test_npm_workspace_missing_file_cannot_be_replaced_by_a_root_file(npm_repo):
    path = _DASHBOARD_TEST_PATHS[0]
    (npm_repo / "dashboard" / path).unlink()
    root_file = npm_repo / path
    root_file.parent.mkdir(parents=True)
    root_file.write_text("// wrong directory\n")
    command = _npm_suite()
    item = {"id": "dashboard", "kind": "test", "target": command}
    assert _met(item, root=npm_repo, tests=[command]) is False


@pytest.mark.parametrize(
    "selector", ["missing", "src", "../dashboard", "dashboard/../dashboard", "/tmp", "*"]
)
def test_npm_workspace_rejects_unknown_unconfigured_and_traversing_selectors(npm_repo, selector):
    command = _npm_suite(f"npm -w {selector} run test")
    item = {"id": "dashboard", "kind": "test", "target": command}
    assert _met(item, root=npm_repo, tests=[command]) is False


@pytest.mark.parametrize(
    "manifest",
    [None, "not json", "[]", '{"workspaces": "dashboard"}', '{"workspaces": ["*"]}'],
)
def test_npm_workspace_requires_a_resolvable_root_manifest(npm_repo, manifest):
    path = npm_repo / "package.json"
    if manifest is None:
        path.unlink()
    else:
        path.write_text(manifest)
    command = _npm_suite()
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
        is False
    )


@pytest.mark.parametrize("manifest", [None, "not json", "[]"])
def test_npm_workspace_requires_its_package_manifest(npm_repo, manifest):
    path = npm_repo / "dashboard/package.json"
    if manifest is None:
        path.unlink()
    else:
        path.write_text(manifest)
    command = _npm_suite()
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
        is False
    )


def test_npm_workspace_missing_directory_is_unmet(repo):
    (repo / "package.json").write_text('{"workspaces": ["dashboard"]}')
    command = "npm -w dashboard run test"
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=repo, tests=[command])
        is False
    )


@pytest.mark.parametrize(
    "command",
    [
        "npm --workspace= run test",
        "npm run test --workspace",
        "npm run test --workspaces",
        "npm -ws run test",
        "npm -w dashboard -w dashboard run test",
        "npm -w dashboard run test --prefix dashboard",
        "npm -w dashboard run test --if-present",
        "npm -w 'dashboard run test",
        "npm -w dashboard run lint",
        "npm -w dashboard run test && npm run lint",
    ],
)
def test_npm_workspace_unsupported_or_malformed_commands_are_unmet(npm_repo, command):
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
        is False
    )


def test_npm_workspace_paths_cannot_traverse_or_follow_symlinks_outside(npm_repo, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.test.ts"
    outside.write_text("// outside the worktree\n")
    escaped = npm_repo / "dashboard/escape.test.ts"
    escaped.symlink_to(outside)
    loop = npm_repo / "dashboard/loop.test.ts"
    loop.symlink_to(loop)
    for path in ("escape.test.ts", "loop.test.ts", str(outside), "../tests/test_a.py"):
        command = f"npm -w dashboard run test -- {path}"
        assert (
            _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
            is False
        )


def test_npm_workspace_directory_cannot_escape_through_a_symlink(npm_repo, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-workspace"
    outside.mkdir()
    (outside / "package.json").write_text('{"name": "escape"}')
    (npm_repo / "escape").symlink_to(outside, target_is_directory=True)
    (npm_repo / "package.json").write_text('{"workspaces": ["escape"]}')
    command = "npm -w escape run test"
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
        is False
    )


def test_npm_workspace_package_name_must_resolve_unambiguously(npm_repo):
    (npm_repo / "other").mkdir()
    (npm_repo / "other/package.json").write_text('{"name": "@queue/dashboard"}')
    (npm_repo / "package.json").write_text('{"workspaces": ["dashboard", "other"]}')
    command = "npm --workspace=@queue/dashboard test"
    assert (
        _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
        is False
    )


def test_npm_root_paths_and_script_workspace_arguments_keep_their_root_context(npm_repo):
    for command in (
        "npm run test -- tests/test_a.py",
        "npm test -- --workspace dashboard tests/test_a.py",
    ):
        assert (
            _met({"id": "suite", "kind": "test", "target": command}, root=npm_repo, tests=[command])
            is True
        )


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


# --- kind=review: the document goes to Reviews, not only a branch ----------
#
# vivid-delta: research task crisp-orbit-37 closed with its proposal committed
# to its branch and never submitted, so Jack saw nothing in the Reviews tab.


class TestReviewDeclaration:
    @pytest.mark.parametrize("target", ["spec", "plan", "other", "any"])
    def test_review_kind_targets_are_stored(self, target):
        items, error = _one("review", target)
        assert error is None
        assert items == [{"id": "item", "kind": "review", "target": target}]

    def test_review_target_must_be_a_review_kind_or_any(self):
        items, error = _one("review", "docs/design/proposal.md")
        assert items == []
        assert error and "'spec', 'plan', 'other' or 'any'" in error


def _review_met(target, reviews):
    item = {"id": "review", "kind": "review", "target": target}
    [result] = evaluate_deliverables([item], root=Path("."), tests=[], reviews=reviews)
    return result["met"]


def test_review_item_is_unmet_without_a_submitted_review():
    assert _review_met("any", []) is False
    assert _review_met("spec", None) is False


def test_review_item_any_is_met_by_any_submitted_review():
    assert _review_met("any", [{"id": "rev-a", "kind": "other", "state": "in_review"}]) is True


def test_review_item_with_a_kind_needs_a_review_of_that_kind():
    reviews = [{"id": "rev-a", "kind": "other", "state": "approved"}]
    assert _review_met("spec", reviews) is False
    assert _review_met("other", reviews) is True


def test_a_withdrawn_review_does_not_meet_a_review_item():
    assert _review_met("any", [{"id": "rev-a", "kind": "spec", "state": "withdrawn"}]) is False


_IMPLICIT_REVIEW = {"id": "review", "kind": "review", "target": "any"}


class TestEffectiveDeliverables:
    @pytest.mark.parametrize("task_type", ["research", "design", TaskType.RESEARCH])
    def test_document_task_types_get_an_implicit_review_item(self, task_type):
        declared = [{"id": "notes", "kind": "file", "target": "docs/notes.md"}]
        assert effective_deliverables(declared, task_type=task_type) == [
            *declared,
            _IMPLICIT_REVIEW,
        ]

    @pytest.mark.parametrize("task_type", [None, "feature", "bugfix", "docs", "plan", "art"])
    def test_other_task_types_keep_their_declared_items(self, task_type):
        assert effective_deliverables([], task_type=task_type) == []

    def test_a_declared_review_item_replaces_the_implicit_one(self):
        declared = [{"id": "proposal", "kind": "review", "target": "spec"}]
        assert effective_deliverables(declared, task_type="research") == declared

    def test_a_feature_task_can_declare_a_review_item(self):
        declared = [{"id": "proposal", "kind": "review", "target": "other"}]
        assert effective_deliverables(declared, task_type="feature") == declared

    def test_a_dispatched_reviewer_answers_with_comments_and_is_exempt(self):
        assert effective_deliverables([], task_type="research", dispatched_reviewer=True) == []

    def test_the_implicit_id_never_collides_with_a_declared_one(self):
        declared = [{"id": "review", "kind": "file", "target": "docs/review.md"}]
        [_, implicit] = effective_deliverables(declared, task_type="design")
        assert implicit == {"id": "review-2", "kind": "review", "target": "any"}

    def test_the_declared_list_is_not_mutated(self):
        declared: list[dict[str, str]] = []
        effective_deliverables(declared, task_type="research")
        assert declared == []
