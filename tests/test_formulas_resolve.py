"""extends chain, merge, var validation, chain hash (spec §13, §16)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.task_graph.formulas import (
    FormulaError,
    FormulaRegistry,
    chain_sha,
    load_from_vault,
    merge_documents,
    resolve_chain,
    resolve_formula,
    validate_vars,
)

FIXTURES = Path(__file__).parent / "fixtures" / "formulas"


@pytest.fixture
def reg(tmp_path):
    vault = tmp_path / "vault"
    (vault / "formulas").mkdir(parents=True)
    (vault / "projects" / "p1" / "formulas").mkdir(parents=True)
    shutil.copy(FIXTURES / "base-review.md", vault / "formulas" / "base-review.md")
    shutil.copy(FIXTURES / "review-and-fix.md", vault / "formulas" / "review-and-fix.md")
    r = FormulaRegistry()
    assert load_from_vault(r, str(vault)) == []
    return r, vault


def test_chain_root_first(reg):
    r, _ = reg
    chain = resolve_chain(r, "review-and-fix", project_id=None)
    assert [f.name for f in chain] == ["base-review", "review-and-fix"]


def test_chain_missing_and_cycle(reg, tmp_path):
    r, vault = reg
    node = "nodes:\n  - key: x\n    title: x"
    (vault / "formulas" / "orphan.md").write_text(
        f"---\nname: orphan\nextends: nope\n---\n```aq-graph\nversion: 1\n{node}\n```\n")
    (vault / "formulas" / "a.md").write_text(
        f"---\nname: a\nextends: b\n---\n```aq-graph\nversion: 1\n{node}\n```\n")
    (vault / "formulas" / "b.md").write_text(
        f"---\nname: b\nextends: a\n---\n```aq-graph\nversion: 1\n{node}\n```\n")
    load_from_vault(r, str(vault))
    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "orphan", project_id=None)
    assert exc.value.errors[0].rule == "formula.extends_missing"
    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "a", project_id=None)
    assert exc.value.errors[0].rule == "formula.extends_cycle"


def test_project_shadow_applies_at_every_hop(reg):
    r, vault = reg
    (vault / "projects" / "p1" / "formulas" / "base-review.md").write_text(
        (FIXTURES / "base-review.md").read_text().replace("Review {branch}", "P1 review {branch}"))
    load_from_vault(r, str(vault))
    chain = resolve_chain(r, "review-and-fix", project_id="p1")
    assert chain[0].scope == "project:p1"


def test_merge_nodes_by_key_child_wins_new_appended(reg):
    r, _ = reg
    doc = merge_documents(resolve_chain(r, "review-and-fix", project_id=None))
    keys = [n["key"] for n in doc["nodes"]]
    assert keys == ["review", "fix"]                      # parent order, child key appended
    review = next(n for n in doc["nodes"] if n["key"] == "review")
    assert review["title"] == "Review branch {branch} (strict)"   # child wins
    assert review["acceptance"] == ["findings written"]             # inherited (child did not set)
    assert doc["parent"]["title"] == "Review and fix {branch}"
    assert doc["defaults"] == {"profile": "{reviewer}"}


def test_validate_vars():
    from src.task_graph.formulas import VarDecl

    decls = {"branch": VarDecl("branch", required=True),
             "reviewer": VarDecl("reviewer", default="reviewer", enum=("reviewer", "coding"))}
    rules = {e.rule for e in validate_vars(decls, {})}
    assert rules == {"formula.var_required"}
    rules = {e.rule for e in validate_vars(decls, {"branch": "x", "reviewer": "nope"})}
    assert rules == {"formula.var_enum"}
    rules = {e.rule for e in validate_vars(decls, {"branch": "x", "bogus": "1"})}
    assert rules == {"formula.var_unknown"}
    assert validate_vars(decls, {"branch": "main"}) == []


def test_resolve_formula_effective_vars_and_sha(reg):
    r, _ = reg
    res = resolve_formula(r, "review-and-fix", project_id=None,
                          supplied_vars={"branch": "feat/x"})
    assert res.findings == []
    assert res.vars == {"branch": "feat/x", "reviewer": "reviewer", "fixer": "coding"}
    assert res.document["vars"] == res.vars
    assert res.chain_sha == chain_sha(res.chain) and len(res.chain_sha) == 64
    res2 = resolve_formula(r, "review-and-fix", project_id=None, supplied_vars={})
    assert [e.rule for e in res2.findings] == ["formula.var_required"]


def test_chain_sha_changes_when_root_changes(reg):
    r, vault = reg
    before = resolve_formula(r, "review-and-fix", project_id=None,
                             supplied_vars={"branch": "b"}).chain_sha
    path = vault / "formulas" / "base-review.md"
    path.write_text(path.read_text().replace("findings written", "findings recorded"))
    load_from_vault(r, str(vault))
    after = resolve_formula(r, "review-and-fix", project_id=None,
                            supplied_vars={"branch": "b"}).chain_sha
    assert before != after
