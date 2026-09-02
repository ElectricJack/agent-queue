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
    merged_var_decls,
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
        f"---\nname: orphan\nextends: nope\n---\n```aq-graph\nversion: 1\n{node}\n```\n"
    )
    (vault / "formulas" / "a.md").write_text(
        f"---\nname: a\nextends: b\n---\n```aq-graph\nversion: 1\n{node}\n```\n"
    )
    (vault / "formulas" / "b.md").write_text(
        f"---\nname: b\nextends: a\n---\n```aq-graph\nversion: 1\n{node}\n```\n"
    )
    load_from_vault(r, str(vault))
    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "orphan", project_id=None)
    assert exc.value.errors[0].rule == "formula.extends_missing"
    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "a", project_id=None)
    assert exc.value.errors[0].rule == "formula.extends_cycle"
    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "ghost", project_id=None)
    assert exc.value.errors[0].rule == "formula.not_found"
    assert exc.value.errors[0].detail == "no formula named 'ghost' in scope system"


def test_project_shadow_applies_at_every_hop(reg):
    r, vault = reg
    (vault / "projects" / "p1" / "formulas" / "base-review.md").write_text(
        (FIXTURES / "base-review.md").read_text().replace("Review {branch}", "P1 review {branch}")
    )
    load_from_vault(r, str(vault))
    chain = resolve_chain(r, "review-and-fix", project_id="p1")
    assert chain[0].scope == "project:p1"


def test_merge_nodes_by_key_child_wins_new_appended(reg):
    r, _ = reg
    doc = merge_documents(resolve_chain(r, "review-and-fix", project_id=None))
    keys = [n["key"] for n in doc["nodes"]]
    assert keys == ["review", "fix"]  # parent order, child key appended
    review = next(n for n in doc["nodes"] if n["key"] == "review")
    assert review["title"] == "Review branch {branch} (strict)"  # child wins
    assert review["acceptance"] == ["findings written"]  # inherited (child did not set)
    assert doc["parent"]["title"] == "Review and fix {branch}"
    assert doc["defaults"] == {
        "profile": "{reviewer}",
        "intelligence_class": "standard-low",
    }


def test_merge_defaults_child_null_does_not_clobber_inherited(reg, tmp_path):
    r, vault = reg
    (vault / "formulas" / "root-defaults.md").write_text(
        "---\nname: root-defaults\n---\n"
        "```aq-graph\nversion: 1\ndefaults:\n  profile: reviewer\n"
        "nodes:\n  - key: x\n    title: x\n```\n"
    )
    (vault / "formulas" / "child-defaults.md").write_text(
        "---\nname: child-defaults\nextends: root-defaults\n---\n"
        "```aq-graph\nversion: 1\ndefaults:\n  profile:\n"
        "nodes:\n  - key: y\n    title: y\n```\n"
    )
    load_from_vault(r, str(vault))
    chain = resolve_chain(r, "child-defaults", project_id=None)
    doc = merge_documents(chain)
    assert doc["defaults"] == {"profile": "reviewer"}  # child's bare `profile:` did not clobber it


def test_validate_vars():
    from src.task_graph.formulas import VarDecl

    decls = {
        "branch": VarDecl("branch", required=True),
        "reviewer": VarDecl("reviewer", default="reviewer", enum=("reviewer", "coding")),
    }
    rules = {e.rule for e in validate_vars(decls, {})}
    assert rules == {"formula.var_required"}
    rules = {e.rule for e in validate_vars(decls, {"branch": "x", "reviewer": "nope"})}
    assert rules == {"formula.var_enum"}
    rules = {e.rule for e in validate_vars(decls, {"branch": "x", "bogus": "1"})}
    assert rules == {"formula.var_unknown"}
    assert validate_vars(decls, {"branch": "main"}) == []


def test_resolve_formula_effective_vars_and_sha(reg):
    r, _ = reg
    res = resolve_formula(r, "review-and-fix", project_id=None, supplied_vars={"branch": "feat/x"})
    assert res.findings == []
    assert res.vars == {"branch": "feat/x", "reviewer": "reviewer", "fixer": "coding"}
    assert res.document["vars"] == res.vars
    assert res.chain_sha == chain_sha(res.chain) and len(res.chain_sha) == 64
    res2 = resolve_formula(r, "review-and-fix", project_id=None, supplied_vars={})
    assert [e.rule for e in res2.findings] == ["formula.var_required"]


def test_chain_sha_changes_when_root_changes(reg):
    r, vault = reg
    before = resolve_formula(
        r, "review-and-fix", project_id=None, supplied_vars={"branch": "b"}
    ).chain_sha
    path = vault / "formulas" / "base-review.md"
    path.write_text(path.read_text().replace("findings written", "findings recorded"))
    load_from_vault(r, str(vault))
    after = resolve_formula(
        r, "review-and-fix", project_id=None, supplied_vars={"branch": "b"}
    ).chain_sha
    assert before != after


def test_three_hop_chain_inherits_omitted_scalars(reg, tmp_path):
    # root sets node `review` priority: 200 and a parent priority: 200; mid
    # (extends root) only adds node `fix`; leaf (extends mid) overrides only
    # `review.title` and only `parent.title` — the omitted scalars (node
    # priority, parent priority) must be inherited from root untouched, since
    # `Formula.graph_doc` now holds only authored keys (no `to_dict()`
    # defaults to clobber them with).
    r, vault = reg
    (vault / "formulas" / "root.md").write_text(
        "---\nname: root\nvars:\n  branch: {required: true}\n---\n"
        "```aq-graph\nversion: 1\n"
        "parent:\n  title: Root parent\n  priority: 200\n"
        "nodes:\n  - key: review\n    title: Review {branch}\n    priority: 200\n"
        "```\n"
    )
    (vault / "formulas" / "mid.md").write_text(
        "---\nname: mid\nextends: root\nvars:\n  branch: {required: true}\n---\n"
        "```aq-graph\nversion: 1\n"
        "nodes:\n  - key: fix\n    title: Fix findings\n    needs: [review]\n"
        "```\n"
    )
    (vault / "formulas" / "leaf.md").write_text(
        "---\nname: leaf\nextends: mid\nvars:\n  branch: {required: true}\n---\n"
        "```aq-graph\nversion: 1\n"
        "parent:\n  title: Leaf parent\n"
        "nodes:\n  - key: review\n    title: Review {branch} (leaf)\n"
        "```\n"
    )
    load_from_vault(r, str(vault))
    chain = resolve_chain(r, "leaf", project_id=None)
    assert [f.name for f in chain] == ["root", "mid", "leaf"]
    doc = merge_documents(chain)
    assert [n["key"] for n in doc["nodes"]] == ["review", "fix"]
    review = next(n for n in doc["nodes"] if n["key"] == "review")
    assert review["title"] == "Review {branch} (leaf)"
    assert review["priority"] == 200  # inherited from root, leaf never wrote it
    assert doc["parent"]["title"] == "Leaf parent"  # leaf wins
    assert doc["parent"]["priority"] == 200  # inherited, leaf's parent block omits it


def test_leaf_redeclares_var_with_tighter_enum(reg, tmp_path):
    r, vault = reg
    (vault / "formulas" / "wide.md").write_text(
        "---\nname: wide\nvars:\n"
        "  reviewer: {default: reviewer, enum: [reviewer, coding, other]}\n---\n"
        "```aq-graph\nversion: 1\nnodes:\n  - key: x\n    title: x\n```\n"
    )
    (vault / "formulas" / "narrow.md").write_text(
        "---\nname: narrow\nextends: wide\nvars:\n"
        "  reviewer: {default: reviewer, enum: [reviewer, coding]}\n---\n"
        "```aq-graph\nversion: 1\nnodes:\n  - key: y\n    title: y\n```\n"
    )
    load_from_vault(r, str(vault))
    chain = resolve_chain(r, "narrow", project_id=None)
    decls = merged_var_decls(chain)
    assert decls["reviewer"].enum == ("reviewer", "coding")  # leaf's tighter enum wins
    errors = validate_vars(decls, {"reviewer": "other"})
    assert [e.rule for e in errors] == ["formula.var_enum"]


# ───────────────── scope-qualified extends: system:<name> ────────────────


def _write_project_override(vault, body_title="P1 review {branch}", extends="system:base-review"):
    """A project formula that shadows AND extends the system one of that name."""
    (vault / "projects" / "p1" / "formulas" / "base-review.md").write_text(
        "---\n"
        "name: base-review\n"
        "description: P1 review\n"
        f"extends: {extends}\n"
        "vars:\n"
        "  branch: {required: true}\n"
        "---\n"
        "```aq-graph\n"
        "version: 1\n"
        "nodes:\n"
        "  - key: review\n"
        f"    title: {body_title}\n"
        "  - key: p1-extra\n"
        "    title: Project-only step\n"
        "```\n"
    )


def test_project_override_extends_same_named_system_formula(reg):
    """`extends: system:base-review` reaches past the shadowing override."""
    r, vault = reg
    _write_project_override(vault)
    load_from_vault(r, str(vault))

    chain = resolve_chain(r, "base-review", project_id="p1")
    assert [f.scope for f in chain] == ["system", "project:p1"]

    doc = merge_documents(chain)
    keys = [n["key"] for n in doc["nodes"]]
    # The system node is inherited (parent order) and the override's own node
    # is appended; the override's change to the shared key wins.
    assert keys == ["review", "p1-extra"]
    review = next(n for n in doc["nodes"] if n["key"] == "review")
    assert review["title"] == "P1 review {branch}"
    # Inherited from the system parent, which the override never restated.
    assert review["acceptance"] == ["findings written"]
    # `defaults` came from the system formula too.
    assert doc["defaults"]["profile"] == "{reviewer}"


def test_extends_scope_and_name_split(reg):
    r, vault = reg
    _write_project_override(vault)
    load_from_vault(r, str(vault))

    override = r.get("base-review", "p1")
    # The raw string is kept verbatim; the parsed parts sit beside it.
    assert override.extends == "system:base-review"
    assert override.extends_scope == "system"
    assert override.extends_name == "base-review"

    system = r.get("base-review", None)
    assert system.extends is None and system.extends_scope is None


def test_unqualified_self_extend_still_rejected(reg, tmp_path):
    """Only the *qualified* form escapes the self-extend check."""
    r, vault = reg
    node = "nodes:\n  - key: x\n    title: x"
    path = vault / "projects" / "p1" / "formulas" / "loop.md"
    path.write_text(f"---\nname: loop\nextends: loop\n---\n```aq-graph\nversion: 1\n{node}\n```\n")
    errors = load_from_vault(r, str(vault))
    assert any("formula.extends_self" in str(e) for e in errors), errors


def test_extends_system_missing_reports_extends_missing(reg):
    r, vault = reg
    (vault / "projects" / "p1" / "formulas" / "nope.md").write_text(
        "---\nname: nope\nextends: system:missing\n---\n"
        "```aq-graph\nversion: 1\nnodes:\n  - key: x\n    title: x\n```\n"
    )
    load_from_vault(r, str(vault))

    with pytest.raises(FormulaError) as exc:
        resolve_chain(r, "nope", project_id="p1")
    assert exc.value.errors[0].rule == "formula.extends_missing"
    assert "system:missing" in exc.value.errors[0].detail


def test_qualified_hop_pins_only_itself(reg):
    """A further unqualified hop resolves project-first again."""
    r, vault = reg
    # system: leaf -> mid ; project: mid (shadow, no extends)
    for scope_dir, name, extends, title in (
        (vault / "formulas", "mid", None, "system mid"),
        (vault / "projects" / "p1" / "formulas", "mid", None, "p1 mid"),
    ):
        (scope_dir / f"{name}.md").write_text(
            f"---\nname: {name}\n---\n```aq-graph\nversion: 1\n"
            f"nodes:\n  - key: m\n    title: {title}\n```\n"
        )
    (vault / "formulas" / "leaf.md").write_text(
        "---\nname: leaf\nextends: mid\n---\n```aq-graph\nversion: 1\n"
        "nodes:\n  - key: l\n    title: leaf\n```\n"
    )
    (vault / "projects" / "p1" / "formulas" / "leaf.md").write_text(
        "---\nname: leaf\nextends: system:leaf\n---\n```aq-graph\nversion: 1\n"
        "nodes:\n  - key: l\n    title: p1 leaf\n```\n"
    )
    load_from_vault(r, str(vault))

    chain = resolve_chain(r, "leaf", project_id="p1")
    assert [(f.name, f.scope) for f in chain] == [
        ("mid", "project:p1"),  # system:leaf's unqualified `extends: mid`
        ("leaf", "system"),  # pinned by the qualifier
        ("leaf", "project:p1"),
    ]


def test_cycle_check_keys_on_scope_and_name(reg):
    """system:X -> X must not be mistaken for a cycle just by name."""
    r, vault = reg
    _write_project_override(vault)
    load_from_vault(r, str(vault))
    # Resolves cleanly: (p1, base-review) and (system, base-review) differ.
    assert len(resolve_chain(r, "base-review", project_id="p1")) == 2


def test_formula_list_shows_extends_verbatim(reg):
    r, vault = reg
    _write_project_override(vault)
    load_from_vault(r, str(vault))

    rows = [
        {"name": f.name, "scope": f.scope, "extends": f.extends} for f in r.list_for_scope("p1")
    ]
    override = next(row for row in rows if row["scope"] == "project:p1")
    assert override["extends"] == "system:base-review"
