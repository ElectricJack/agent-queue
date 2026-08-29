"""Formula files: frontmatter, aq-graph block, registry, vault loading (spec §13)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.task_graph.formulas import (
    FORMULA_PATTERNS,
    Formula,
    FormulaError,
    FormulaRegistry,
    derive_formula_id,
    load_from_vault,
    parse_formula,
)

FIXTURES = Path(__file__).parent / "fixtures" / "formulas"


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "formulas").mkdir(parents=True)
    (root / "projects" / "p1" / "formulas").mkdir(parents=True)
    return root


def write_formula(vault, rel, text):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestParse:
    def test_parses_fixture(self):
        text = (FIXTURES / "base-review.md").read_text()
        f = parse_formula(text, rel_path="formulas/base-review.md")
        assert isinstance(f, Formula)
        assert (f.name, f.scope, f.project_id, f.extends) == ("base-review", "system", None, None)
        assert f.vars["branch"].required is True
        assert f.vars["reviewer"].default == "reviewer"
        assert f.vars["reviewer"].enum == ("reviewer", "coding")
        assert f.graph_doc["nodes"][0]["key"] == "review"
        assert len(f.content_sha) == 64

    def test_project_scope_from_path(self):
        text = (FIXTURES / "review-and-fix.md").read_text()
        f = parse_formula(text, rel_path="projects/p1/formulas/review-and-fix.md")
        assert (f.scope, f.project_id, f.extends) == ("project:p1", "p1", "base-review")

    @pytest.mark.parametrize(
        "mutate, rule",
        [
            (lambda t: t.replace("name: base-review", "name: other"), "formula.name_mismatch"),
            (lambda t: t.split("```aq-graph")[0], "formula.no_graph"),
            (lambda t: t + "\n```aq-graph\nversion: 1\nnodes: []\n```\n", "formula.multiple_graphs"),
            (lambda t: t.replace("version: 1", "version: 1\nvars: {branch: x}"), "formula.vars_in_body"),
            (lambda t: t.replace("branch: {required: true}", "branch: {required: true, default: x}"),
             "formula.var_decl"),
            (lambda t: t.replace("enum: [reviewer, coding]", "enum: [coding]"), "formula.var_decl"),
            (lambda t: t.replace("---\n# Base", "extends: base-review\n---\n# Base"),
             "formula.extends_self"),
            (lambda t: t.replace("version: 1", "version: [1"), "formula.graph_parse"),
        ],
    )
    def test_rejects(self, mutate, rule):
        text = mutate((FIXTURES / "base-review.md").read_text())
        with pytest.raises(FormulaError) as exc:
            parse_formula(text, rel_path="formulas/base-review.md")
        assert rule in {e.rule for e in exc.value.errors}

    def test_missing_frontmatter(self):
        with pytest.raises(FormulaError) as exc:
            parse_formula("# no frontmatter\n```aq-graph\nversion: 1\n```\n", rel_path="formulas/x.md")
        assert {e.rule for e in exc.value.errors} == {"formula.frontmatter"}


class TestRegistry:
    def test_derive_id(self):
        assert derive_formula_id("formulas/base-review.md") == (None, "base-review")
        assert derive_formula_id("projects/p1/formulas/x.md") == ("p1", "x")
        assert derive_formula_id("projects/p1/specs/x.md") is None
        assert FORMULA_PATTERNS == ["formulas/*.md", "projects/*/formulas/*.md"]

    def test_project_shadows_system(self, vault):
        shutil.copy(FIXTURES / "base-review.md", vault / "formulas" / "base-review.md")
        write_formula(vault, "projects/p1/formulas/base-review.md",
                      (FIXTURES / "base-review.md").read_text().replace(
                          "description: Review a branch", "description: Project flavour"))
        reg = FormulaRegistry()
        assert load_from_vault(reg, str(vault)) == []
        assert reg.get("base-review").description == "Review a branch"
        assert reg.get("base-review", "p1").description == "Project flavour"
        assert reg.get("base-review", "p2").description == "Review a branch"
        names = [(f.scope, f.name) for f in reg.list_for_scope("p1")]
        assert names == [("project:p1", "base-review")]

    def test_parse_error_is_collected_not_raised(self, vault):
        write_formula(vault, "formulas/bad.md", "---\nname: bad\n---\nno block\n")
        shutil.copy(FIXTURES / "base-review.md", vault / "formulas" / "base-review.md")
        reg = FormulaRegistry()
        errors = load_from_vault(reg, str(vault))
        assert len(errors) == 1 and "formulas/bad.md" in errors[0]
        assert reg.get("bad") is None and reg.get("base-review") is not None
        assert "formulas/bad.md" in reg.errors

    async def test_watcher_keeps_previous_on_bad_edit(self, vault):
        from src.task_graph.formulas import _on_formula_changed
        from src.vault_watcher import VaultChange

        path = vault / "formulas" / "base-review.md"
        shutil.copy(FIXTURES / "base-review.md", path)
        reg = FormulaRegistry()
        load_from_vault(reg, str(vault))
        path.write_text("---\nname: base-review\n---\nhalf saved", encoding="utf-8")
        await _on_formula_changed(
            [VaultChange(path=str(path), rel_path="formulas/base-review.md", operation="modified")],
            registry=reg, vault_root=str(vault))
        assert reg.get("base-review") is not None  # previous good entry kept
        assert "formulas/base-review.md" in reg.errors
        path.unlink()
        await _on_formula_changed(
            [VaultChange(path=str(path), rel_path="formulas/base-review.md", operation="deleted")],
            registry=reg, vault_root=str(vault))
        assert reg.get("base-review") is None
