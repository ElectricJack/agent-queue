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
