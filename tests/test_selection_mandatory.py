"""The mandatory set ``M`` and the reason codes selection records carry.

``M`` is what must run whatever any model says (spec §4.2): changed tests,
reviewed ownership and source-scanning rules, scoped conftests, direct
importers of a changed ``src`` file and the critical ratchets.  A global
invalidator, an incomplete snapshot or an unmapped path makes it the whole
universe.  Pure functions over the fixture project's committed artifacts.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from src.test_selection import catalogue as cat
from src.test_selection import reasons
from src.test_selection.catalogue import load_catalogue, load_rules
from src.test_selection.mandatory import mandatory_set
from src.test_selection.snapshot import ChangedPath, ChangeSnapshot
from tests.selection_fixture_repo import FILES, build_fixture_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure functions over dataclasses."""


@pytest.fixture
def repo(tmp_path):
    return build_fixture_repo(tmp_path / "repo")


@pytest.fixture
def world(repo):
    catalogue = load_catalogue(repo / "tests/selection_catalogue.json")
    return catalogue, load_rules(repo / "tests/selection_rules.yaml", catalogue)


def snap(*changes: ChangedPath, complete=True, reason=None) -> ChangeSnapshot:
    return ChangeSnapshot(
        workspace="/w",
        base_ref="origin/main",
        base_sha="b" * 40,
        head_sha="h" * 40,
        changes=changes,
        dirty_fingerprint="d",
        complete=complete,
        incomplete_reason=reason,
        incomplete_detail=None,
        taken_at=0.0,
    )


# --------------------------------------------------------------------------
# Reason codes


def test_every_reason_is_pinned():
    assert reasons.ALL_REASONS >= {
        "mandatory_rule",
        "jev_unknown",
        "jev_confident_unaffected",
        "fallback_timeout",
    }
    assert (
        reasons.with_detail(reasons.GLOBAL_INVALIDATOR, "pyproject.toml")
        == "global_invalidator:pyproject.toml"
    )


def test_all_reasons_is_exactly_the_module_constants():
    constants = {
        value
        for name, value in vars(reasons).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    assert constants == reasons.ALL_REASONS
    # A code never holds the detail separator, so ``code:detail`` splits unambiguously.
    assert all(code.replace("_", "").isalnum() and code.islower() for code in reasons.ALL_REASONS)


def test_with_detail_refuses_an_unknown_code_or_an_empty_detail():
    with pytest.raises(ValueError, match="unknown reason code"):
        reasons.with_detail("made_up", "x")
    with pytest.raises(ValueError, match="detail"):
        reasons.with_detail(reasons.UNMAPPED_PATH, "")


def test_code_of_strips_the_detail():
    assert reasons.code_of("mandatory_rule:gamma") == "mandatory_rule"
    assert reasons.code_of("unmapped_path:src/a:b.py") == "unmapped_path"
    assert reasons.code_of("mandatory_critical") == "mandatory_critical"


# --------------------------------------------------------------------------
# The plan's cases


def test_a_changed_source_file_adds_its_owner_area_direct_importers_and_critical(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("src/pkg/c.py", "modified")), catalogue, rules)
    assert not result.full_required
    assert result.modules == {"tests/test_c.py", "tests/test_a.py"}  # gamma owner + critical
    assert "mandatory_rule:gamma" in result.reasons["tests/test_c.py"]
    assert "mandatory_direct_import:src/pkg/c.py" in result.reasons["tests/test_c.py"]
    assert result.reasons["tests/test_a.py"] == ("mandatory_critical",)
    assert result.affected_areas == {"gamma"}


def test_a_changed_test_module_is_mandatory_and_a_deleted_one_names_its_area(world):
    catalogue, rules = world
    result = mandatory_set(
        snap(
            ChangedPath("tests/test_c.py", "deleted"),
            ChangedPath("tests/sub/test_b.py", "modified"),
        ),
        catalogue,
        rules,
    )
    assert "tests/test_c.py" not in result.modules
    assert "tests/sub/test_b.py" in result.modules
    assert result.affected_areas >= {"gamma", "beta"}


def test_a_global_invalidator_makes_m_the_universe(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("pyproject.toml", "modified")), catalogue, rules)
    assert result.full_required and result.modules == catalogue.universe
    assert result.global_reasons == ("global_invalidator:pyproject.toml",)


def test_an_incomplete_snapshot_is_full(world):
    catalogue, rules = world
    result = mandatory_set(snap(complete=False, reason="unknown_base"), catalogue, rules)
    assert result.full_required and result.global_reasons == ("snapshot_incomplete:unknown_base",)


def test_a_scoped_conftest_adds_its_subtree_only(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("tests/sub/conftest.py", "modified")), catalogue, rules)
    assert result.modules == {"tests/sub/test_b.py", "tests/test_a.py"}
    assert (
        "mandatory_scoped_conftest:tests/sub/conftest.py" in result.reasons["tests/sub/test_b.py"]
    )


def test_non_behavioral_paths_add_nothing_and_never_force_fallback(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("README.md", "modified")), catalogue, rules)
    assert result.modules == frozenset() and not result.full_required


def test_docs_changes_reach_the_scanning_test_through_ownership_and_source_scan(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("docs/README.md", "modified")), catalogue, rules)
    assert "tests/test_docs_scan.py" in result.modules
    assert set(result.reasons["tests/test_docs_scan.py"]) >= {
        "mandatory_rule:docs-guards",
        "mandatory_source_scan",
    }


def test_an_unmapped_production_path_forces_full(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("src/pkg/data.json", "modified")), catalogue, rules)
    assert result.full_required and result.unmapped_paths == ("src/pkg/data.json",)
    assert result.global_reasons == ("unmapped_path:src/pkg/data.json",)


def test_a_renamed_source_file_maps_both_sides(world):
    catalogue, rules = world
    result = mandatory_set(
        snap(ChangedPath("src/pkg/d.py", "renamed", old_path="src/pkg/b.py")), catalogue, rules
    )
    assert result.full_required  # d.py is unmapped; b.py's owner still counted
    assert "beta" in result.affected_areas and "unmapped_path:src/pkg/d.py" in result.global_reasons


# --------------------------------------------------------------------------
# Removed paths, renames and surviving modules


def _with_second_gamma_module(repo: Path):
    """The fixture with ``tests/test_c2.py`` added to area gamma, catalogue regenerated."""
    (repo / "tests/test_c2.py").write_text(
        "from src.pkg.c import gamma\n\n\ndef test_gamma_again():\n    assert gamma() == 3\n"
    )
    areas_path = repo / cat.AREAS_PATH
    areas_path.write_text(
        areas_path.read_text().replace('match: ["tests/test_c.py"]', 'match: ["tests/test_c*.py"]')
    )
    built = cat.build_catalogue(repo, cat.load_areas(areas_path))
    (repo / cat.CATALOGUE_PATH).write_text(cat.render_catalogue(built))
    catalogue = load_catalogue(repo / cat.CATALOGUE_PATH)
    return catalogue, load_rules(repo / cat.RULES_PATH, catalogue)


def test_a_deleted_test_module_puts_its_areas_surviving_modules_in_m(repo):
    """Review focus 3: the deleted path runs nowhere; its area's other modules must run."""
    catalogue, rules = _with_second_gamma_module(repo)
    assert catalogue.areas["gamma"].modules == ("tests/test_c.py", "tests/test_c2.py")
    result = mandatory_set(snap(ChangedPath("tests/test_c.py", "deleted")), catalogue, rules)
    assert not result.full_required
    assert "tests/test_c.py" not in result.modules
    assert "tests/test_c2.py" in result.modules
    assert "mandatory_changed_test:tests/test_c.py" in result.reasons["tests/test_c2.py"]
    assert "tests/test_c.py" not in result.reasons
    assert result.affected_areas == {"gamma"}


def test_a_renamed_test_module_runs_under_its_new_name_only(repo):
    catalogue, rules = _with_second_gamma_module(repo)
    result = mandatory_set(
        snap(ChangedPath("tests/test_c2.py", "renamed", old_path="tests/test_c.py")),
        catalogue,
        rules,
    )
    assert "tests/test_c.py" not in result.modules
    assert "mandatory_changed_test" in result.reasons["tests/test_c2.py"]
    assert result.affected_areas == {"gamma"}


def test_a_module_the_snapshot_removed_never_enters_m_through_a_rule(world):
    catalogue, rules = world
    result = mandatory_set(
        snap(ChangedPath("src/pkg/c.py", "modified"), ChangedPath("tests/test_c.py", "deleted")),
        catalogue,
        rules,
    )
    assert not result.full_required
    assert result.modules == {"tests/test_a.py"}  # gamma's only module is gone
    assert result.affected_areas == {"gamma"}


def test_a_deleted_source_file_still_adds_its_owners_and_importers(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("src/pkg/c.py", "deleted")), catalogue, rules)
    assert not result.full_required
    assert {"mandatory_rule:gamma", "mandatory_direct_import:src/pkg/c.py"} <= set(
        result.reasons["tests/test_c.py"]
    )


def test_an_untracked_test_module_is_a_changed_test(world):
    catalogue, rules = world
    result = mandatory_set(snap(ChangedPath("tests/test_c.py", "untracked")), catalogue, rules)
    assert result.reasons["tests/test_c.py"] == ("mandatory_changed_test",)


# --------------------------------------------------------------------------
# Invariants


def test_full_results_explain_every_module(world):
    catalogue, rules = world
    for changes, code in [
        (
            (ChangedPath("pyproject.toml", "modified"), ChangedPath("src/pkg/c.py", "modified")),
            "global_invalidator",
        ),
        ((ChangedPath("src/pkg/data.json", "modified"),), "unmapped_path"),
        ((), "snapshot_incomplete"),
    ]:
        result = mandatory_set(
            snap(*changes, complete=bool(changes), reason="git_error"), catalogue, rules
        )
        assert result.full_required and result.modules == catalogue.universe
        assert set(result.reasons) == catalogue.universe
        for module, why in result.reasons.items():
            assert code in why, module
            assert len(why) == len(set(why)), module


def test_every_reason_is_a_pinned_code(world):
    catalogue, rules = world
    paths = [*FILES, "src/pkg/d.py", "README.md", "tests/sub/conftest.py"]
    for path in paths:
        result = mandatory_set(snap(ChangedPath(path, "modified")), catalogue, rules)
        for why in [*result.reasons.values(), result.global_reasons]:
            assert {reasons.code_of(r) for r in why} <= reasons.ALL_REASONS, path
        assert set(result.reasons) == set(result.modules), path


def test_the_result_does_not_depend_on_change_order(world):
    catalogue, rules = world
    changes = [
        ChangedPath("src/pkg/c.py", "modified"),
        ChangedPath("docs/README.md", "modified"),
        ChangedPath("src/pkg/data.json", "modified"),
        ChangedPath("src/pkg/zz.py", "added"),
        ChangedPath("tests/sub/conftest.py", "modified"),
    ]
    result = mandatory_set(snap(*changes), catalogue, rules)
    for order in itertools.permutations(changes):
        again = mandatory_set(snap(*order), catalogue, rules)
        assert again == result and list(again.reasons.items()) == list(result.reasons.items())
    assert result.unmapped_paths == ("src/pkg/data.json", "src/pkg/zz.py")


def test_unmapped_agrees_with_the_rules_coverage_check(world):
    """``validate_rules_cover_tree`` and ``mandatory_set`` must call the same paths unmapped."""
    catalogue, rules = world
    imported = frozenset(name for info in catalogue.modules.values() for name in info.imports)
    candidates = [
        *FILES,
        "tests/selection_catalogue.json",
        "src/pkg/d.py",
        "src/pkg/__init__.py",
        "docs/guide/x.md",
        "notes.txt",
        "tests/sub/helpers.py",
        "README.md",
    ]
    for path in candidates:
        result = mandatory_set(snap(ChangedPath(path, "modified")), catalogue, rules)
        mapped = cat._is_mapped(path, rules, catalogue, imported)
        assert (path in result.unmapped_paths) is (not mapped), path


def test_the_committed_artifacts_map_a_change_to_this_module():
    """The repository's own rules: this library's source reaches its tests without a full run."""
    catalogue = load_catalogue(ROOT / cat.CATALOGUE_PATH)
    rules = load_rules(ROOT / cat.RULES_PATH, catalogue)
    result = mandatory_set(
        snap(ChangedPath("src/test_selection/mandatory.py", "modified")), catalogue, rules
    )
    assert not result.full_required
    assert "tests/test_selection_mandatory.py" in result.modules
    assert {
        "mandatory_rule:test-selection",
        "mandatory_direct_import:src/test_selection/mandatory.py",
    } <= set(result.reasons["tests/test_selection_mandatory.py"])
    assert set(rules.critical) <= result.modules
    full = mandatory_set(snap(ChangedPath("src/config.py", "modified")), catalogue, rules)
    assert full.full_required and full.global_reasons == ("global_invalidator:src/config.py",)
