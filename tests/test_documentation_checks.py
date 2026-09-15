"""Focused contracts for the dependency-free documentation checker."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-docs.py"


def _checker():
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_check_links_accepts_github_relative_paths_and_duplicate_heading_anchors(tmp_path, monkeypatch):
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "target.md"
    target.write_text("# Same heading\n\n## Same heading\n", encoding="utf-8")
    source = docs / "source.md"
    source.write_text("[first](target.md#same-heading) [second](target.md#same-heading-1)\n", encoding="utf-8")

    assert checker.check_links([source]) == []


def test_check_links_reports_missing_anchor_and_ignores_fenced_examples(tmp_path, monkeypatch):
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Present\n", encoding="utf-8")
    source = docs / "source.md"
    source.write_text(
        "[bad](target.md#missing)\n\n```markdown\n[example](missing.md)\n```\n",
        encoding="utf-8",
    )

    assert checker.check_links([source]) == [
        "docs/source.md: target.md#missing: anchor #missing does not exist",
    ]


def test_check_links_accepts_github_relative_directory_links_but_not_directory_anchors(
    tmp_path,
    monkeypatch,
):
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    (docs / "reference").mkdir(parents=True)
    source = docs / "source.md"
    source.write_text("[directory](reference/)\n", encoding="utf-8")

    assert checker.check_links([source]) == []

    source.write_text("[bad directory anchor](reference/#missing)\n", encoding="utf-8")
    assert checker.check_links([source]) == [
        "docs/source.md: reference/#missing: directory targets have no anchors",
    ]


def test_module_coverage_requires_linked_rows_and_exclusion_reasons(tmp_path, monkeypatch):
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("", encoding="utf-8")
    catalog = tmp_path / "docs" / "reference" / "modules" / "topic.md"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("| Module |\n| --- |\n| [src/thing.py](../../../src/thing.py) |\n", encoding="utf-8")
    manifest = {
        "shards": {"topic": {"catalog": "docs/reference/modules/topic.md"}},
        "modules": {
            "src/thing.py": {
                "shard": "topic",
                "category": "production",
                "catalog": "docs/reference/modules/topic.md",
                "purpose": "Example module.",
            },
            "notes/history.md": {
                "shard": "topic",
                "category": "documentation",
                "catalog": None,
                "purpose": "Historical material.",
            },
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert checker.check_module_coverage(manifest_path, "topic") == []
    manifest["modules"]["notes/history.md"]["purpose"] = ""
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert checker.check_module_coverage(manifest_path, "topic") == [
        "notes/history.md: intentional exclusion/category has no reason",
    ]


def test_github_anchor_matches_githubs_own_slug_for_the_cases_that_bite():
    """Slugs verified against GitHub's ``POST /markdown`` renderer.

    Each of these produced a wrong answer once: an underscore inside an
    identifier is a word character GitHub keeps, while an underscore acting as
    an emphasis delimiter disappears, and a removed character such as ``+`` or
    ``&`` leaves a space behind that becomes its own hyphen.
    """
    checker = _checker()

    assert checker.github_anchor("`stale_claim`: the task moved on") == (
        "stale_claim-the-task-moved-on"
    )
    assert checker.github_anchor("8. `memory_save` flow") == "8-memory_save-flow"
    assert checker.github_anchor("Windows + WSL2 quickstart") == "windows--wsl2-quickstart"
    assert checker.github_anchor("6. Memory Health & Observability") == (
        "6-memory-health--observability"
    )
    assert checker.github_anchor("_Italic_ heading") == "italic-heading"
    assert checker.github_anchor("**Bold** heading") == "bold-heading"


def test_check_links_leaves_line_anchors_on_source_files_alone(tmp_path, monkeypatch):
    """``file.py#L109`` is GitHub's source-view line anchor, not a heading slug."""
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("[line](../src/thing.py#L109)\n", encoding="utf-8")

    assert checker.check_links([source]) == []

    source.write_text("[gone](../src/missing.py#L109)\n", encoding="utf-8")
    assert checker.check_links([source]) == [
        "docs/source.md: ../src/missing.py#L109: target does not exist",
    ]
