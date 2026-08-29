# Swarm Work Model — Plan 3: Formulas

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reusable, project-shadowed workflow templates ("formulas") — markdown files with declared vars and an `aq-graph` block — that cook into task graphs with full provenance, so the same review/fix/verify shapes can be stamped out by humans and agents alike (beads property P7, "workflows as data").

**Architecture:** A `FormulaRegistry` (in-memory, vault-watched, same shape as `HarnessRegistry`) holds parsed `Formula` objects. Pure functions resolve an `extends` chain, merge graph documents, validate/apply vars and hash the chain. `formula_cook` feeds the resolved document into the existing graph pipeline (`parse_graph` → `validate_graph` → `build_plan` → `write_plan`), and `write_plan` gains an optional `provenance` bundle written inside its single transaction (metadata keys, a `formula_snapshot` context row, a `formula:<name>` label). Three commands (`formula_list`, `formula_show`, `formula_cook`) expose it; `formula_show --as-cooked` re-renders from the snapshot.

**Tech Stack:** Python 3.12, SQLAlchemy Core 2.x async, PyYAML, Click, pytest-asyncio (auto), ruff (line-length 100).

**Spec:** `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part III (§13) plus the formula rows of §14 (surface), §16 (tests: `extends` merge, var validation, shadowing, cycle detection; doctor `formulas.parse`; invariant tests), §17 (Part III is the third independently mergeable plan). Plans 1 and 2 are on branch `swarm/hierarchy`; this plan continues on it.

## Global Constraints

- Formula files: `vault/formulas/<name>.md` (system) and `vault/projects/<pid>/formulas/<name>.md` (project shadows system by `name`). Frontmatter keys: `name` (must equal the file stem), `description`, `vars` (map of `{required?: bool, default?: str, enum?: [str]}`), `extends` (single parent, same scope resolution). Body: exactly one fenced `aq-graph` block; a `vars:` key inside the block is an error (`formula.vars_in_body`) — vars are DECLARED in frontmatter and SUPPLIED at cook time (spec §13).
- Resolution order at cook (spec §13): load `extends` chain root-first (cycle → `formula.extends_cycle`; missing → `formula.extends_missing`) → merge (`parent` field-wise, child wins; `nodes` by `key`, child wins, new keys appended in child order; `defaults` key-wise) → validate declared vars against supplied values (`formula.var_required`, `formula.var_enum`, `formula.var_unknown`) → apply defaults → `substitute_vars` → `validate_graph` → `create_graph`.
- Provenance is written IN the graph-creation transaction (spec §13): `task_metadata` on the container: `formula=<name>`, `formula_scope=system|project:<pid>`, `formula_path=<vault-relative path of the leaf>`, `formula_vars=<json of the effective vars>`, `formula_chain_sha=<sha256 over the chain's file contents root→leaf>`; one `task_context` row `type='formula_snapshot'`, `label=<name>`, `content=<json of the resolved graph document post-extends, post-vars, pre-id>`; label `formula:<name>` on the container. `formula_show --as-cooked <container>` renders from the snapshot, never from the current file; it performs NO writes.
- `formula_cook` with `dry_run` writes nothing and returns the same report shape with `dry_run: true` plus the provenance that WOULD be written.
- Registry parse errors never crash the daemon: logged, kept in `registry.errors`, surfaced by `aq doctor` (`formulas.parse`, WARN, report-only); the watcher keeps the previous good entry on a failed re-parse (same as harnesses).
- No new tables or columns. Perf: `formula_cook` at `PLAN_NODES=200` ≤ `write_plan` budget + 3 statements (one meta upsert, one context insert, one label insert) = `3*N + 23`; `formula_show` (not cooked) performs no DB writes and ≤ 4 statements (validation reads only).
- Every new `_cmd_*` has a tool definition and a response model; `formula_list`/`formula_show` join `AGENT_COMMAND_SET`, `formula_cook` does not (spec §14: "cook no"). New event `formula.cooked` is registered with a canonical payload.
- Full suite command for every task: `timeout 580 pytest tests/ --ignore=tests/chat_eval -n auto -q -p no:cacheprovider` in the FOREGROUND with the Bash tool `timeout` parameter 600000. Known-flaky under load: `tests/test_tmux_integration.py::TestNudge::test_nudge_does_not_ratchet_activity`, `tests/test_claim_commands.py::TestClaim::test_wait_wakes_on_task_ready` — re-run alone if one fails. Never `git stash`; never `git commit -a`; never run Alembic against the default DB (no schema change in this plan anyway).
- `ruff check` on touched files, `ruff format` on new files; commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Controller rulings folded into this plan

1. **`vars` values are strings.** `substitute_vars` is textual; declared defaults and supplied values are coerced with `str()`; `enum` entries compared as strings.
2. **Frontmatter parsing reuses `parse_frontmatter`** (`src/profiles/parser.py:212`); `vars`/`extends`/`description` are read from `ProfileFrontmatter.extra`. No new YAML loader.
3. **Provenance rides `write_plan`** via an optional `provenance: FormulaProvenance | None` parameter (raw inserts, like the existing `task_context`/`task_labels` writes in that function) — `add_task_label`/`add_task_context` do not take `conn=` and are not used inside the transaction.
4. **`formula_cook` returns the `create_task_graph` report plus a `provenance` block**; the container id is `report["parent_id"]`.
5. **CLI:** `aq formula list` is auto-generated; `aq formula show` and `aq formula cook` are hand-crafted so `--var k=v` can repeat (auto-generation would expose a JSON `--vars`). The hand-crafted names are added to `HANDCRAFTED_COVERAGE` in `src/cli/auto_commands.py`.
6. **Scope:** a session-scoped caller may `formula_list`/`formula_show` for its own project only; `formula_cook` is elevated/local only (not in `AGENT_COMMAND_SET`), so worker filing of whole graphs stays a human/supervisor act for now (spec §14).

---

## File structure

| File | Responsibility |
|---|---|
| `src/task_graph/formulas.py` (**new**) | `VarDecl`, `Formula`, `FormulaError`, `parse_formula`, `FormulaRegistry`, vault loading + watcher handlers, `resolve_chain`, `merge_documents`, `validate_vars`, `chain_sha`, `resolve_formula` |
| `src/task_graph/creator.py` | `FormulaProvenance`; `write_plan(..., provenance=)`, `create_graph(..., provenance=)` |
| `src/commands/formula_commands.py` (**new**) | `FormulaCommandsMixin`: `_cmd_formula_list`, `_cmd_formula_show`, `_cmd_formula_cook` |
| `src/commands/handler.py` | register the mixin |
| `src/orchestrator/core.py` | `self.formula_registry`; load + watcher handlers next to the harness registry |
| `src/doctor/formula_checks.py` (**new**), `src/doctor/__init__.py` | `formulas.parse` |
| `src/event_schemas.py` | `formula.cooked` |
| `src/tools/definitions.py`, `src/api/models/task.py`, `src/api/scope.py`, `src/cli/formulas.py` (**new**), `src/cli/app.py`, `src/cli/auto_commands.py` | surface |
| `docs/specs/design/formulas.md` (**new**), `docs/superpowers/specs/…design.md` §18, `CLAUDE.md`, `profile.md`, `src/skills/aq-tasks/SKILL.md` | docs |
| `tests/test_formulas_parse.py`, `tests/test_formulas_resolve.py`, `tests/test_formulas_provenance.py`, `tests/test_formula_commands.py`, `tests/test_formula_doctor.py`, `tests/test_formula_surface.py`, `tests/perf/test_formula_statements.py`, `tests/fixtures/formulas/*.md` | tests |

**Shared fixture shapes** (copy where needed):

```python
# Registry-only tests (no DB)
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


# Handler tests (real sqlite + real vault dir) — mirrors tests/test_create_task_graph_command.py
@pytest.fixture
async def setup(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from src.commands.handler import CommandHandler
    from src.database import Database
    from src.models import AgentProfile, Project
    from src.task_graph.formulas import FormulaRegistry, load_from_vault

    db = Database(str(tmp_path / "f.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))
    for pid in ("coding", "reviewer"):
        await db.create_profile(AgentProfile(id=pid, name=pid))
    vault_root = tmp_path / "vault"
    (vault_root / "formulas").mkdir(parents=True)
    (vault_root / "projects" / "p1" / "formulas").mkdir(parents=True)
    registry = FormulaRegistry()
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    orch.bus.emit = AsyncMock()
    orch.formula_registry = registry
    config = MagicMock()
    config.vault_root = str(vault_root)
    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    yield handler, db, vault_root, registry
    await db.close()
```

Fixture formulas (`tests/fixtures/formulas/`):

```markdown
<!-- base-review.md -->
---
name: base-review
description: Review a branch
vars:
  branch: {required: true}
  reviewer: {default: reviewer, enum: [reviewer, coding]}
---
# Base review

```aq-graph
version: 1
defaults: { profile: "{reviewer}" }
parent: { title: "Review {branch}" }
nodes:
  - key: review
    title: Review branch {branch}
    acceptance: ["findings written"]
```
```

```markdown
<!-- review-and-fix.md -->
---
name: review-and-fix
description: Review a branch, fix findings, re-review
extends: base-review
vars:
  fixer: {default: coding}
---
# Review and fix

```aq-graph
version: 1
parent: { title: "Review and fix {branch}" }
nodes:
  - key: fix
    title: Fix findings on {branch}
    profile: "{fixer}"
    needs: [review]
    acceptance: ["all findings addressed"]
  - key: review
    title: Review branch {branch} (strict)
```
```

---

### Task 1: Formula file format, parser, registry, vault loading

**Files:**
- Create: `src/task_graph/formulas.py` (part 1: models, `parse_formula`, `FormulaRegistry`, `FORMULA_PATTERNS`, `derive_formula_id`, `load_from_vault`, `register_formula_handlers`)
- Create: `tests/fixtures/formulas/base-review.md`, `tests/fixtures/formulas/review-and-fix.md`
- Test: `tests/test_formulas_parse.py`

**Interfaces:**
- Consumes: `parse_frontmatter(text) -> (ProfileFrontmatter, body)` (`src/profiles/parser.py:212`; `.extra` holds `description`/`vars`/`extends`), `extract_graph_block(markdown) -> str | None` (`src/task_graph/parser.py`), `parse_graph(source, fmt="auto") -> TaskGraph` / `GraphParseError` / `GraphError(rule, detail, node=None, severity="error")` (`src/task_graph/models.py`), `VaultWatcher.register_handler(pattern, handler, handler_id=)`, `VaultChange(path, rel_path, operation)`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class VarDecl:
      name: str
      required: bool = False
      default: str | None = None
      enum: tuple[str, ...] | None = None

  @dataclass
  class Formula:
      name: str
      description: str
      scope: str                 # "system" | "project:<pid>"
      project_id: str | None
      rel_path: str              # vault-relative, e.g. "formulas/base-review.md"
      vars: dict[str, VarDecl]
      extends: str | None
      graph_block: str           # raw text of the aq-graph block
      graph_doc: dict            # parsed block (JSON/YAML → dict), unvalidated
      content_sha: str           # sha256 of the whole file text

  class FormulaError(Exception):
      def __init__(self, errors: list[GraphError]): ...
      errors: list[GraphError]

  def parse_formula(text: str, *, rel_path: str) -> Formula   # raises FormulaError
  FORMULA_PATTERNS = ["formulas/*.md", "projects/*/formulas/*.md"]
  def derive_formula_id(rel_path: str) -> tuple[str | None, str] | None   # (project_id, name)
  def vault_path_for(vault_root: str, name: str, project_id: str | None) -> str

  class FormulaRegistry:
      errors: dict[str, str]     # rel_path -> message, for doctor
      def upsert(self, formula: Formula) -> None
      def remove(self, name: str, project_id: str | None = None) -> bool
      def clear(self) -> None
      def get(self, name: str, project_id: str | None = None) -> Formula | None   # project shadows system
      def list_for_scope(self, project_id: str | None = None) -> list[Formula]
      def list_all(self) -> list[Formula]

  def load_from_vault(registry: FormulaRegistry, vault_root: str) -> list[str]   # full reload; returns error strings
  def register_formula_handlers(watcher, registry: FormulaRegistry, *, vault_root: str) -> list[str]
  ```
  Parse rules (rule ids on `GraphError.rule`): `formula.frontmatter` (missing/invalid frontmatter or `name` absent), `formula.name_mismatch` (`name` ≠ file stem), `formula.no_graph` (no `aq-graph` block), `formula.multiple_graphs` (>1 block), `formula.graph_parse` (block not JSON/YAML mapping), `formula.vars_in_body` (`vars` key inside the block), `formula.var_decl` (a var entry is not a mapping, `enum` not a list of scalars, `default` ∉ `enum`, `required` with a `default`), `formula.extends_self` (`extends == name`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formulas_parse.py
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
```

Check `VaultChange`'s constructor fields in `src/vault_watcher.py` and adjust the two constructions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_formulas_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: src.task_graph.formulas`.

- [ ] **Step 3: Implement `formulas.py` (part 1)**

```python
"""Formulas — reusable task-graph templates in the vault (swarm-work-model §13).

A formula is a markdown file with YAML frontmatter (``name``, ``description``,
``vars`` declarations, optional single ``extends``) and exactly one fenced
``aq-graph`` block.  Files live at ``vault/formulas/<name>.md`` (system) or
``vault/projects/<pid>/formulas/<name>.md`` (project shadows system by name).
The registry is in-memory and vault-watched, like ``HarnessRegistry``; a file
that fails to parse is logged, remembered in ``registry.errors`` for
``aq doctor``, and — on a watcher re-parse — leaves the previous good entry
in place so a half-saved edit never takes a formula offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field

from src.profiles.parser import parse_frontmatter
from src.task_graph.models import GraphError
from src.task_graph.parser import GRAPH_FENCE_LANG, extract_graph_block, parse_graph, GraphParseError

logger = logging.getLogger(__name__)

FORMULA_PATTERNS: list[str] = ["formulas/*.md", "projects/*/formulas/*.md"]
_SYSTEM_RE = re.compile(r"^formulas/([^/]+)\.md$")
_PROJECT_RE = re.compile(r"^projects/([^/]+)/formulas/([^/]+)\.md$")
_FENCE_COUNT_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*" + GRAPH_FENCE_LANG + r"[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class VarDecl:
    name: str
    required: bool = False
    default: str | None = None
    enum: tuple[str, ...] | None = None


@dataclass
class Formula:
    name: str
    description: str
    scope: str
    project_id: str | None
    rel_path: str
    vars: dict[str, VarDecl]
    extends: str | None
    graph_block: str
    graph_doc: dict
    content_sha: str


class FormulaError(Exception):
    def __init__(self, errors: list[GraphError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.rule}: {e.detail}" for e in errors))


def derive_formula_id(rel_path: str) -> tuple[str | None, str] | None:
    rel = rel_path.replace(os.sep, "/")
    m = _SYSTEM_RE.match(rel)
    if m:
        return None, m.group(1)
    m = _PROJECT_RE.match(rel)
    if m:
        return m.group(1), m.group(2)
    return None


def vault_path_for(vault_root: str, name: str, project_id: str | None) -> str:
    if project_id:
        return os.path.join(vault_root, "projects", project_id, "formulas", f"{name}.md")
    return os.path.join(vault_root, "formulas", f"{name}.md")


def _parse_var_decls(raw, errors: list[GraphError]) -> dict[str, VarDecl]:
    out: dict[str, VarDecl] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        errors.append(GraphError(rule="formula.var_decl", detail="vars must be a mapping"))
        return out
    for name, spec in raw.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            errors.append(GraphError(rule="formula.var_decl", detail=f"{name}: declaration must be a mapping"))
            continue
        required = bool(spec.get("required", False))
        default = spec.get("default")
        default = None if default is None else str(default)
        enum = spec.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not all(isinstance(v, (str, int, float, bool)) for v in enum):
                errors.append(GraphError(rule="formula.var_decl", detail=f"{name}: enum must be a list of scalars"))
                continue
            enum = tuple(str(v) for v in enum)
        if required and default is not None:
            errors.append(GraphError(rule="formula.var_decl", detail=f"{name}: required vars cannot have a default"))
            continue
        if enum is not None and default is not None and default not in enum:
            errors.append(GraphError(rule="formula.var_decl", detail=f"{name}: default {default!r} not in enum"))
            continue
        out[str(name)] = VarDecl(name=str(name), required=required, default=default, enum=enum)
    return out


def parse_formula(text: str, *, rel_path: str) -> Formula:
    errors: list[GraphError] = []
    ident = derive_formula_id(rel_path)
    stem = ident[1] if ident else os.path.splitext(os.path.basename(rel_path))[0]
    project_id = ident[0] if ident else None
    fm, body = parse_frontmatter(text)
    if body == text or not fm.name:
        raise FormulaError([GraphError(rule="formula.frontmatter",
                                       detail="frontmatter with a `name` is required")])
    if fm.name != stem:
        errors.append(GraphError(rule="formula.name_mismatch",
                                 detail=f"name {fm.name!r} does not match file stem {stem!r}"))
    extends = fm.extra.get("extends")
    extends = str(extends) if extends else None
    if extends == fm.name:
        errors.append(GraphError(rule="formula.extends_self", detail="a formula cannot extend itself"))
    var_decls = _parse_var_decls(fm.extra.get("vars"), errors)
    fences = _FENCE_COUNT_RE.findall(body)
    if len(fences) == 0:
        errors.append(GraphError(rule="formula.no_graph", detail="exactly one aq-graph block is required"))
    elif len(fences) > 1:
        errors.append(GraphError(rule="formula.multiple_graphs", detail=f"{len(fences)} aq-graph blocks"))
    graph_doc: dict = {}
    block = extract_graph_block(body) or ""
    if block:
        try:
            graph_doc = parse_graph(block).to_dict()
        except GraphParseError as exc:
            errors.append(GraphError(rule="formula.graph_parse",
                                     detail="; ".join(e.detail for e in exc.errors)))
        else:
            if "vars" in graph_doc and graph_doc["vars"]:
                errors.append(GraphError(rule="formula.vars_in_body",
                                         detail="declare vars in frontmatter, not in the aq-graph block"))
    if errors:
        raise FormulaError(errors)
    return Formula(
        name=fm.name, description=str(fm.extra.get("description") or ""),
        scope=f"project:{project_id}" if project_id else "system", project_id=project_id,
        rel_path=rel_path.replace(os.sep, "/"), vars=var_decls, extends=extends,
        graph_block=block, graph_doc=graph_doc,
        content_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
```

Note: `parse_graph(block).to_dict()` may re-serialise `vars` as `{}` even when absent — check the raw block text for a top-level `vars:` key instead if `to_dict()` always emits it (`yaml.safe_load(block)` for the check is acceptable since `parse_graph` already succeeded). Also confirm `parse_frontmatter` returns the ORIGINAL text as the body when there is no frontmatter (that is what the `body == text` test relies on).

Registry + vault loading — copy `HarnessRegistry`'s shape from `src/sessions/harness_registry.py` exactly (dict keyed `(project_id, name)`, `get` tries project then system, `list_for_scope` returns project entries plus unshadowed system entries, sorted by name), add `errors: dict[str, str]`; `load_from_vault` walks `FORMULA_PATTERNS` under `vault_root` with `glob`, clears, parses each file (`rel_path` = path relative to `vault_root`), collects `f"{rel}: {exc}"` strings and fills `registry.errors`; `_on_formula_changed(changes, *, registry, vault_root)` handles `deleted` (remove + drop error) and `created|modified` (parse; on success upsert + drop error; on failure keep the previous entry and record the error); `register_formula_handlers(watcher, registry, *, vault_root)` registers one handler per pattern with `handler_id=f"formula:{pattern}"` and returns the ids.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_formulas_parse.py tests/test_task_graph.py tests/test_harness_parser.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/formulas.py tests/test_formulas_parse.py tests/fixtures/formulas/base-review.md tests/fixtures/formulas/review-and-fix.md
git commit -m "feat(formulas): formula file format, parser, in-memory registry, vault loading and watcher handlers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Chain resolution, document merge, var validation, chain hash

**Files:**
- Modify: `src/task_graph/formulas.py` (part 2: `resolve_chain`, `merge_documents`, `validate_vars`, `apply_defaults`, `chain_sha`, `ResolvedFormula`, `resolve_formula`)
- Test: `tests/test_formulas_resolve.py`

**Interfaces:**
- Consumes: Task 1's `Formula`, `FormulaRegistry`, `VarDecl`, `GraphError`.
- Produces:
  ```python
  def resolve_chain(registry: FormulaRegistry, name: str, *, project_id: str | None) -> list[Formula]
      # root first; raises FormulaError formula.extends_missing / formula.extends_cycle; each hop resolved with the SAME project_id (project shadows system at every level)
  def merge_documents(chain: list[Formula]) -> dict
      # returns a NEW dict: version=1; `defaults` merged key-wise child wins; `parent` field-wise child wins (child keys override, missing keys inherited); `nodes` merged by `key` — child node replaces the parent node's fields it sets (field-wise, child wins; `needs`, `labels`, `acceptance`, `context` REPLACED not concatenated when the child sets them), new keys appended in child order; `spec` child wins
  def validate_vars(decls: dict[str, VarDecl], supplied: dict[str, str]) -> list[GraphError]
      # formula.var_required (declared required, absent), formula.var_enum (value not in enum), formula.var_unknown (supplied but not declared anywhere in the chain)
  def apply_defaults(decls: dict[str, VarDecl], supplied: dict[str, str]) -> dict[str, str]
      # supplied (str()-coerced) over defaults; only declared names
  def merged_var_decls(chain: list[Formula]) -> dict[str, VarDecl]   # child redeclaration wins
  def chain_sha(chain: list[Formula]) -> str   # sha256 over "\n".join(f.content_sha for f in chain)  (root→leaf)

  @dataclass
  class ResolvedFormula:
      leaf: Formula
      chain: list[Formula]
      vars: dict[str, str]        # effective values
      document: dict              # merged, with "vars": effective values injected for substitute_vars
      chain_sha: str
      findings: list[GraphError]  # var validation findings (errors block cooking)

  def resolve_formula(registry, name, *, project_id, supplied_vars) -> ResolvedFormula   # raises FormulaError only for chain problems; var problems land in .findings
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formulas_resolve.py
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
    (vault / "formulas" / "orphan.md").write_text(
        "---\nname: orphan\nextends: nope\n---\n```aq-graph\nversion: 1\nnodes: []\n```\n")
    (vault / "formulas" / "a.md").write_text(
        "---\nname: a\nextends: b\n---\n```aq-graph\nversion: 1\nnodes: []\n```\n")
    (vault / "formulas" / "b.md").write_text(
        "---\nname: b\nextends: a\n---\n```aq-graph\nversion: 1\nnodes: []\n```\n")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_formulas_resolve.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_chain'`.

- [ ] **Step 3: Implement**

```python
def resolve_chain(registry, name, *, project_id):
    chain: list[Formula] = []
    seen: list[str] = []
    current = name
    while current is not None:
        if current in seen:
            raise FormulaError([GraphError(rule="formula.extends_cycle",
                                           detail=" -> ".join(seen + [current]))])
        f = registry.get(current, project_id)
        if f is None:
            raise FormulaError([GraphError(rule="formula.extends_missing",
                                           detail=f"{current!r} (required by {seen[-1] if seen else name!r})")])
        seen.append(current)
        chain.append(f)
        current = f.extends
    chain.reverse()
    return chain


def merge_documents(chain):
    import copy

    doc: dict = {"version": 1, "defaults": {}, "parent": {}, "nodes": []}
    index: dict[str, int] = {}
    for f in chain:
        src = copy.deepcopy(f.graph_doc)
        if src.get("spec"):
            doc["spec"] = src["spec"]
        doc["defaults"].update(src.get("defaults") or {})
        doc["parent"].update({k: v for k, v in (src.get("parent") or {}).items() if v not in (None, [], "")})
        for node in src.get("nodes") or []:
            key = node["key"]
            clean = {k: v for k, v in node.items() if v not in (None, [], "")}
            if key in index:
                doc["nodes"][index[key]].update(clean)
            else:
                index[key] = len(doc["nodes"])
                doc["nodes"].append(clean)
    if not doc["parent"]:
        doc.pop("parent")
    return doc
```

`merged_var_decls` (root→leaf, child redeclares), `validate_vars`, `apply_defaults`, `chain_sha`, `resolve_formula` per Interfaces; `resolve_formula` builds `document = merge_documents(chain); document["vars"] = effective` and returns findings from `validate_vars` (do not raise for var problems). Note `to_dict()` of a `TaskGraph` may serialise node `needs` as `[{"on": ..., "dep_type": ...}]` — `merge_documents` must treat that shape as opaque (replace, never concatenate).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_formulas_resolve.py tests/test_formulas_parse.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/formulas.py tests/test_formulas_resolve.py
git commit -m "feat(formulas): extends chain resolution, document merge, var validation, chain hash

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Provenance inside `write_plan`, `formula.cooked` event

**Files:**
- Modify: `src/task_graph/creator.py` (`FormulaProvenance`, `write_plan(db, plan, *, provenance=None)`, `create_graph(..., provenance=None)`, `build_report` adds `provenance` when given)
- Modify: `src/event_schemas.py` (`formula.cooked`), `tests/test_event_schema_registry_validation.py` (canonical payload)
- Test: `tests/test_formulas_provenance.py`

**Interfaces:**
- Consumes: `write_plan` internals (raw inserts on `task_context`/`task_labels`), `db._upsert_meta_many(task_id, items, *, conn)` (`hierarchy_queries.py`), `db.get_task_contexts(task_id)`, `db.get_task_labels(task_id)`, `get_task_metadata`/meta reader used in Plan 2 tests (`get_task_meta(task_id, key)`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class FormulaProvenance:
      name: str
      scope: str            # system | project:<pid>
      path: str             # vault-relative leaf path
      vars: dict[str, str]
      chain_sha: str
      snapshot: dict        # resolved document (post-extends, post-vars, pre-id)

      def metadata(self) -> dict[str, str]:   # formula, formula_scope, formula_path, formula_vars(json), formula_chain_sha
      @property
      def label(self) -> str:                 # f"formula:{self.name}"
  ```
  `write_plan(db, plan, *, provenance: FormulaProvenance | None = None)`: after the label insert and before `recompute_blocked`, when `provenance` is given and `plan.parent_row is not None` (a NEW container): `_upsert_meta_many(plan.parent_id, provenance.metadata(), conn=conn)`; `insert(task_context)` one row `{id: uuid[:12], task_id: parent_id, type: "formula_snapshot", label: name, content: json.dumps(snapshot, sort_keys=True)}`; `insert(task_labels)` `{task_id: parent_id, label: provenance.label}` (skip if already in `unique`). When cooking under an EXISTING container (`parent_id` given, `plan.parent_row is None`), provenance is written to that container the same way (the spec says "the container carries" it) — but only the metadata keys and the snapshot; a container cooked twice keeps the latest metadata and accumulates snapshot rows (one per cook), which `formula_show --as-cooked` reads newest-first. `create_graph(handler, graph, *, project_id, dry_run=False, parent_id=None, provenance=None)` passes it through; `build_report` includes `"provenance": {name, scope, path, vars, chain_sha}` when given (never the snapshot — it can be large).
  Event: `"formula.cooked": {"required": ["container_id", "project_id", "formula", "scope", "chain_sha"], "optional": ["parent_id", "node_count"]}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formulas_provenance.py
"""Provenance is written in the graph-creation transaction (spec §13)."""

from __future__ import annotations

import json

import pytest

from src.database import Database
from src.models import Project
from src.task_graph.creator import FormulaProvenance, build_plan, create_graph, write_plan
from src.task_graph.parser import parse_graph

PROJECT_ID = "proj"
GRAPH = {"version": 1, "parent": {"title": "Epic"},
         "nodes": [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "needs": [{"on": "a"}]}]}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


def prov(**over):
    base = dict(name="review-and-fix", scope="system", path="formulas/review-and-fix.md",
                vars={"branch": "feat/x"}, chain_sha="ab" * 32, snapshot=GRAPH)
    base.update(over)
    return FormulaProvenance(**base)


async def test_new_container_carries_provenance(db):
    plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
    await write_plan(db, plan, provenance=prov())
    cid = plan.parent_id
    assert await db.get_task_meta(cid, "formula") == "review-and-fix"
    assert await db.get_task_meta(cid, "formula_scope") == "system"
    assert await db.get_task_meta(cid, "formula_path") == "formulas/review-and-fix.md"
    assert json.loads(await db.get_task_meta(cid, "formula_vars")) == {"branch": "feat/x"}
    assert await db.get_task_meta(cid, "formula_chain_sha") == "ab" * 32
    ctx = [c for c in await db.get_task_contexts(cid) if c["type"] == "formula_snapshot"]
    assert len(ctx) == 1 and json.loads(ctx[0]["content"])["nodes"][1]["key"] == "b"
    assert "formula:review-and-fix" in await db.get_task_labels(cid)


async def test_provenance_rolls_back_with_the_graph(db, monkeypatch):
    plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)

    async def boom(*a, **k):
        raise RuntimeError("blocked recompute failed")

    monkeypatch.setattr(db, "recompute_blocked", boom)
    with pytest.raises(RuntimeError):
        await write_plan(db, plan, provenance=prov())
    assert await db.get_task(plan.parent_id) is None
    assert await db.get_task_contexts(plan.parent_id) == []


async def test_existing_container_gets_latest_metadata_and_accumulates_snapshots(db):
    first = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
    await write_plan(db, first, provenance=prov(chain_sha="11" * 32))
    second = await build_plan(db, parse_graph({"version": 1, "nodes": [{"key": "c", "title": "C"}]}),
                              project_id=PROJECT_ID, parent_id=first.parent_id)
    await write_plan(db, second, provenance=prov(chain_sha="22" * 32))
    assert await db.get_task_meta(first.parent_id, "formula_chain_sha") == "22" * 32
    snaps = [c for c in await db.get_task_contexts(first.parent_id) if c["type"] == "formula_snapshot"]
    assert len(snaps) == 2


async def test_create_graph_report_and_dry_run(db):
    class H:
        pass

    h = H()
    h.db = db
    report = await create_graph(h, parse_graph(GRAPH), project_id=PROJECT_ID, dry_run=True,
                                provenance=prov())
    assert report["dry_run"] is True and report["provenance"]["name"] == "review-and-fix"
    assert "snapshot" not in report["provenance"]
    assert await db.get_task(report["parent_id"]) is None
    report = await create_graph(h, parse_graph(GRAPH), project_id=PROJECT_ID, provenance=prov())
    assert await db.get_task_meta(report["parent_id"], "formula") == "review-and-fix"
```

Use the real meta reader name (Plan 2 tests used `db.get_task_meta(task_id, key)` — confirm).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_formulas_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name 'FormulaProvenance'`.

- [ ] **Step 3: Implement**

Add `FormulaProvenance` to `creator.py`; thread `provenance` through `write_plan`/`create_graph`/`build_report` as specified; register `formula.cooked` in a new `_FORMULA_SCHEMAS` dict merged into `EVENT_SCHEMAS`, with `_CANONICAL_PAYLOADS["formula.cooked"] = {"container_id": "t-1", "project_id": "proj-1", "formula": "review-and-fix", "scope": "system", "chain_sha": "ab"*32}` in the registry test. Provenance writes use the same `conn` and raw `insert(...)` as the surrounding code — never `add_task_label`/`add_task_context`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_formulas_provenance.py tests/test_hierarchy_graph_creator.py tests/test_create_task_graph_command.py tests/test_event_schema_registry_validation.py tests/perf/test_hierarchy_statements.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/creator.py src/event_schemas.py tests/test_event_schema_registry_validation.py tests/test_formulas_provenance.py
git commit -m "feat(formulas): provenance metadata, snapshot and label written inside write_plan; formula.cooked event

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Commands `formula_list` / `formula_show` / `formula_cook`, orchestrator wiring, doctor check

**Files:**
- Create: `src/commands/formula_commands.py`
- Modify: `src/commands/handler.py` (register `FormulaCommandsMixin`)
- Modify: `src/orchestrator/core.py:458` (`self.formula_registry = FormulaRegistry()` next to `harness_registry`), `:1467-1469` (after `register_harness_handlers`: `load_from_vault(self.formula_registry, self.config.vault_root)` logging errors, then `register_formula_handlers(self.vault_watcher, self.formula_registry, vault_root=self.config.vault_root)`)
- Create: `src/doctor/formula_checks.py`; modify `src/doctor/__init__.py` (`default_registry` registers `formula_checks()`)
- Test: `tests/test_formula_commands.py`, `tests/test_formula_doctor.py`

**Interfaces:**
- Consumes: Tasks 1–3; `parse_graph`, `validate_graph(graph, *, project_id, db, vault_root)`, `split_findings`, `create_graph(..., provenance=)`; `_cmd_create_task_graph`'s `parent_id` validation block (`task_commands.py:1533+` — factor it into a reusable `_validate_graph_parent(project_id, parent_id) -> dict | None` helper on the task mixin and call it from both places); `self._current_scope`; `db.get_task_contexts`.
- Produces:
  - `_cmd_formula_list(args) -> {"success": True, "formulas": [{"name", "description", "scope", "extends", "vars": {name: {"required", "default", "enum"}}, "path"}]}` — `project_id?` (session scope: forced to the scope's project); sorted by name; system entries shadowed by a project entry are omitted for that project.
  - `_cmd_formula_show(args)` — args `name`, `project_id?`, `vars?: dict`, `as_cooked?: <container_id>`. Without `as_cooked`: resolve (`resolve_formula`), build `TaskGraph` from `document`, run `validate_graph` (dry — reads only), return `{"success": True, "name", "scope", "path", "chain": [names], "chain_sha", "vars": {"declared": {...}, "effective": {...}}, "graph": document-after-substitution (`graph.to_dict()`), "errors": [...], "warnings": [...]}` (`success` False when var errors or graph errors). With `as_cooked`: read the container's newest `formula_snapshot` context row + the `formula_*` metadata, return the same shape with `"as_cooked": container_id`, `graph` = the snapshot, `chain_sha` from metadata; no registry access, no validation, no writes; missing snapshot → `{"success": False, "error": "no formula snapshot on <id>"}`.
  - `_cmd_formula_cook(args)` — args `name`, `project_id` (required), `vars?`, `parent_id?`, `dry_run?`. Session-scoped callers → `{"success": False, "error": "formula_cook is not available to agent sessions"}` (belt; scope.py is the braces). Flow: resolve → var findings are errors → `parse_graph(document)` → `validate_graph` → errors → return the `create_task_graph` error envelope; else `_validate_graph_parent`; then `create_graph(self, graph, project_id=, dry_run=, parent_id=, provenance=FormulaProvenance(name, scope, path=leaf.rel_path, vars=effective, chain_sha, snapshot=document-after-substitution))`; on a real write emit `formula.cooked` (post-commit) and return the report with `"success": True`, `"container_id": report["parent_id"]`, `warnings`.
  - Doctor: `formulas.parse` — `WARN` with `data={"count": n, "files": {...}}` when `handler.orchestrator.formula_registry.errors` is non-empty (or when the registry is absent → `INFO` "registry not loaded"), `OK` otherwise; report-only (`fix=None`); owner `"swarm-work-model"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formula_commands.py
"""formula_list / formula_show / formula_cook (spec §13, §14)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.task_graph.formulas import FormulaRegistry, load_from_vault

FIXTURES = Path(__file__).parent / "fixtures" / "formulas"


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))
    for pid in ("coding", "reviewer"):
        await db.create_profile(AgentProfile(id=pid, name=pid))
    vault_root = tmp_path / "vault"
    (vault_root / "formulas").mkdir(parents=True)
    (vault_root / "projects" / "p1" / "formulas").mkdir(parents=True)
    for name in ("base-review.md", "review-and-fix.md"):
        shutil.copy(FIXTURES / name, vault_root / "formulas" / name)
    registry = FormulaRegistry()
    assert load_from_vault(registry, str(vault_root)) == []
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    orch.bus.emit = AsyncMock()
    orch.formula_registry = registry
    config = MagicMock()
    config.vault_root = str(vault_root)
    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    yield handler, db, vault_root, registry
    await db.close()


class TestList:
    async def test_lists_with_scope_and_vars(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_list({"project_id": "p1"})
        names = {f["name"]: f for f in res["formulas"]}
        assert set(names) == {"base-review", "review-and-fix"}
        assert names["review-and-fix"]["extends"] == "base-review"
        assert names["base-review"]["vars"]["branch"] == {"required": True, "default": None, "enum": None}
        assert names["base-review"]["scope"] == "system"

    async def test_session_scope_pins_project(self, setup):
        h, *_ = setup
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
        res = await h._cmd_formula_list({"project_id": "other"})
        assert res["success"] and all(f["scope"] in ("system", "project:p1") for f in res["formulas"])


class TestShow:
    async def test_show_resolves_and_substitutes(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x"}})
        assert res["success"] is True
        assert res["chain"] == ["base-review", "review-and-fix"]
        assert res["vars"]["effective"] == {"branch": "feat/x", "reviewer": "reviewer", "fixer": "coding"}
        titles = {n["key"]: n["title"] for n in res["graph"]["nodes"]}
        assert titles == {"review": "Review branch feat/x (strict)", "fix": "Fix findings on feat/x"}
        assert res["errors"] == []

    async def test_show_reports_missing_required_var(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "review-and-fix", "project_id": "p1"})
        assert res["success"] is False
        assert [e["rule"] for e in res["errors"]] == ["formula.var_required"]

    async def test_show_unknown_formula(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "nope", "project_id": "p1"})
        assert res["success"] is False and "nope" in res["error"]


class TestCook:
    async def test_cook_creates_graph_with_provenance_and_event(self, setup):
        h, db, vault_root, _ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x", "fixer": "coding"}})
        assert res["success"] is True
        cid = res["container_id"]
        assert (await db.get_task(cid)).status == TaskStatus.IN_PROGRESS
        assert await db.get_task_meta(cid, "formula") == "review-and-fix"
        assert await db.get_task_meta(cid, "formula_path") == "formulas/review-and-fix.md"
        assert json.loads(await db.get_task_meta(cid, "formula_vars"))["branch"] == "feat/x"
        assert "formula:review-and-fix" in await db.get_task_labels(cid)
        kids = await db.get_children(cid)
        assert {k.title for k in kids} == {"Review branch feat/x (strict)", "Fix findings on feat/x"}
        fix = next(k for k in kids if k.title.startswith("Fix"))
        assert fix.profile_id == "coding" and fix.is_blocked is True
        emitted = [c.args for c in h.orchestrator.bus.emit.await_args_list if c.args[0] == "formula.cooked"]
        assert emitted and emitted[0][1]["container_id"] == cid

    async def test_cook_dry_run_writes_nothing(self, setup):
        h, db, *_ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "b"}, "dry_run": True})
        assert res["success"] and res["dry_run"] is True and res["provenance"]["name"] == "review-and-fix"
        assert await db.list_tasks("p1") == []

    async def test_cook_var_errors_block(self, setup):
        h, db, *_ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "b", "reviewer": "nobody"}})
        assert res["success"] is False and res["errors"][0]["rule"] == "formula.var_enum"
        assert await db.list_tasks("p1") == []

    async def test_cook_under_parent(self, setup):
        h, db, *_ = setup
        first = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                           "vars": {"branch": "b"}})
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                         "vars": {"branch": "c"}, "parent_id": first["container_id"]})
        assert res["success"] is True and res["container_id"] == first["container_id"]
        assert len(await db.get_children(first["container_id"])) == 2

    async def test_session_scope_refused(self, setup):
        h, *_ = setup
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1", "vars": {"branch": "b"}})
        assert res["success"] is False


class TestAsCooked:
    async def test_as_cooked_renders_snapshot_not_current_file(self, setup):
        h, db, vault_root, registry = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x"}})
        cid = res["container_id"]
        path = vault_root / "formulas" / "review-and-fix.md"
        path.write_text(path.read_text().replace("Fix findings on {branch}", "CHANGED {branch}"))
        load_from_vault(registry, str(vault_root))
        shown = await h._cmd_formula_show({"as_cooked": cid})
        assert shown["success"] and shown["as_cooked"] == cid
        titles = {n["key"]: n["title"] for n in shown["graph"]["nodes"]}
        assert titles["fix"] == "Fix findings on feat/x"
        assert shown["chain_sha"] == await db.get_task_meta(cid, "formula_chain_sha")
        assert await h._cmd_formula_show({"as_cooked": "nope"}) == {
            "success": False, "error": "no formula snapshot on nope"}
```

```python
# tests/test_formula_doctor.py
from unittest.mock import MagicMock

import pytest

from src.doctor import formula_checks
from src.doctor.models import Severity
from src.task_graph.formulas import FormulaRegistry


def _ctx(errors):
    reg = FormulaRegistry()
    reg.errors.update(errors)
    ctx = MagicMock()
    ctx.handler.orchestrator.formula_registry = reg
    return ctx


def test_check_registered():
    assert {c.id for c in formula_checks.formula_checks()} == {"formulas.parse"}
    assert all(c.owner == "swarm-work-model" and c.fix is None for c in formula_checks.formula_checks())


async def test_ok_when_no_errors():
    check = formula_checks.formula_checks()[0]
    assert (await check.run(_ctx({}))).severity == Severity.OK


async def test_warn_lists_files():
    check = formula_checks.formula_checks()[0]
    res = await check.run(_ctx({"formulas/bad.md": "formula.no_graph: ..."}))
    assert res.severity == Severity.WARN and res.data["count"] == 1
    assert "formulas/bad.md" in res.data["files"]
```

Check `db.get_children`'s real name/signature (Plan 1) and the `DoctorContext` attribute path to the orchestrator (`ctx.handler.orchestrator` — confirm in `src/doctor/models.py`/`pool_checks.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_formula_commands.py tests/test_formula_doctor.py -v`
Expected: FAIL — `_cmd_formula_list` missing; `src.doctor.formula_checks` missing.

- [ ] **Step 3: Implement**

`formula_commands.py` per Interfaces. Key pieces:

```python
def _registry(self):
    reg = getattr(self.orchestrator, "formula_registry", None)
    if reg is None:
        raise RuntimeError("formula registry not loaded")
    return reg


def _scope_project(self, args) -> str | None:
    scope = self._current_scope or {}
    if scope.get("kind") == "session" and not scope.get("elevated"):
        return scope.get("project_id")
    return args.get("project_id") or self._active_project_id


async def _resolved_graph(self, name, project_id, supplied):
    """resolve → TaskGraph with vars injected → validate. Returns (resolved, graph, errors, warnings)."""
    res = resolve_formula(self._registry(), name, project_id=project_id, supplied_vars=supplied or {})
    errors = [e for e in res.findings if e.is_error]
    graph = parse_graph(res.document)
    findings = await validate_graph(graph, project_id=project_id, db=self.db,
                                    vault_root=getattr(self.config, "vault_root", None))
    more_errors, warnings = split_findings(findings)
    return res, graph, errors + more_errors, warnings
```

`validate_graph` runs `substitute_vars` on the graph in place, so after it `graph.to_dict()` is the post-substitution document used for `formula_show.graph` and for the provenance `snapshot`. Wire the registry in `Orchestrator.__init__` and the loader/handlers in `initialize()` right after the harness handlers (log each `load_from_vault` error at WARNING). Doctor module mirrors `pool_checks.py`. Add `FormulaCommandsMixin` to `CommandHandler`'s bases. Factor `_cmd_create_task_graph`'s `parent_id` checks into `_validate_graph_parent(project_id, parent_id)` (returns an error dict or None) and use it from both commands.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_formula_commands.py tests/test_formula_doctor.py tests/test_create_task_graph_command.py tests/test_doctor.py tests/test_orchestrator.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/commands/formula_commands.py src/commands/handler.py src/commands/task_commands.py src/orchestrator/core.py src/doctor/formula_checks.py src/doctor/__init__.py tests/test_formula_commands.py tests/test_formula_doctor.py
git commit -m "feat(formulas): formula_list/show/cook commands, as-cooked rendering, registry wiring, formulas.parse doctor check

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Surface, scope, perf budget, docs, crosswalk

**Files:**
- Modify: `src/tools/definitions.py` (`formula_list`, `formula_show`, `formula_cook`; category `formula`; `_CLI_CATEGORY_OVERRIDES` if needed), `src/api/models/task.py` (`FormulaSummary`, `FormulaListResponse`, `FormulaShowResponse`, `FormulaCookResponse` in `RESPONSE_MODELS`), `src/api/scope.py` (`AGENT_COMMAND_SET` += `formula_list`, `formula_show`; `tests/test_api_scope.py` updated)
- Create: `src/cli/formulas.py` (hand-crafted `aq formula show` / `aq formula cook` with repeatable `--var k=v`, `--parent`, `--dry-run`, `--as-cooked`); modify `src/cli/app.py` (register the group before auto commands), `src/cli/auto_commands.py` (`CATEGORY_CLI_NAMES["formula"] = "formula"`, `HANDCRAFTED_COVERAGE` += `formula_show`, `formula_cook`), `src/cli/formatter_registry.py` (list table; show renders chain/vars/errors then the graph nodes; cook prints container id + node ids)
- Create: `tests/perf/test_formula_statements.py`; `tests/test_formula_surface.py`
- Create: `docs/specs/design/formulas.md`; modify `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` §18 (P7 row → implemented, paths), `CLAUDE.md` Quick Reference (Formulas bullet), `profile.md`, `src/skills/aq-tasks/SKILL.md` (a short "cook a formula" snippet; `formula_cook` is not agent-scoped — say so)
- Test: drift guards `tests/test_command_surface.py`, `tests/test_response_model_registry.py`, `tests/test_tool_registry.py`, `tests/test_docs_sync.py` (extend, never weaken)

**Interfaces:**
- Consumes: Task 4 commands; `count_statements`, `seed_scale`, `PLAN_NODES`, `_graph(n)` from `tests/perf/test_hierarchy_statements.py`.
- Produces: tool definitions with `input_schema` for the three commands (`vars` as `object` of string values); `aq formula list [-p]`, `aq formula show <name> [-p] [--var k=v]… | --as-cooked <container>`, `aq formula cook <name> -p <pid> [--var k=v]… [--parent <id>] [--dry-run]`; perf test: a 200-node formula (generated into the tmp vault from `_graph(PLAN_NODES)` with `{branch}` in every title) cooked at `seed_scale` → `≤ 3*PLAN_NODES + 23` statements and ≤ 4.0 s; `formula_show` ≤ 4 statements (the `validate_graph` profile/needs reads) and 0 writes (assert with `count_statements` and a `before_cursor_execute` hook that fails on any `INSERT|UPDATE|DELETE`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formula_surface.py
from __future__ import annotations

from click.testing import CliRunner

from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES


def defs():
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


def test_definitions_present():
    d = defs()
    for name in ("formula_list", "formula_show", "formula_cook"):
        assert name in d and _TOOL_CATEGORIES[name] == "formula"
    assert {"name", "project_id", "vars", "parent_id", "dry_run"} <= set(
        d["formula_cook"]["input_schema"]["properties"])
    assert {"name", "as_cooked", "vars"} <= set(d["formula_show"]["input_schema"]["properties"])


def test_agent_scope():
    from src.api.scope import AGENT_COMMAND_SET

    assert {"formula_list", "formula_show"} <= AGENT_COMMAND_SET
    assert "formula_cook" not in AGENT_COMMAND_SET


def test_cli_cook_collects_vars(monkeypatch):
    from src.cli import formulas as cli_formulas
    from src.cli.main import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, command, args):
            sent.update(command=command, args=args)
            return {"success": True, "container_id": "c1", "task_ids": [], "nodes": []}

    monkeypatch.setattr(cli_formulas, "_get_client", lambda *a, **k: FakeClient())
    r = CliRunner().invoke(cli, ["formula", "cook", "review-and-fix", "-p", "p1",
                                 "--var", "branch=feat/x", "--var", "fixer=coding", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert sent["command"] == "formula_cook"
    assert sent["args"]["vars"] == {"branch": "feat/x", "fixer": "coding"}
    assert sent["args"]["dry_run"] is True


def test_response_models_registered():
    from src.api.models.task import RESPONSE_MODELS

    for name in ("formula_list", "formula_show", "formula_cook"):
        assert name in RESPONSE_MODELS
```

```python
# tests/perf/test_formula_statements.py
"""formula_cook is write_plan + 3 statements (spec §15); formula_show writes nothing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from sqlalchemy import event

from src.commands.handler import CommandHandler
from src.task_graph.formulas import FormulaRegistry, load_from_vault
from tests.perf.test_hierarchy_statements import PLAN_NODES, PROJECT_ID, _graph, count_statements, db, seed_scale  # noqa: F401


def _formula_text(n):
    doc = _graph(n)
    for node in doc["nodes"]:
        node["title"] = node["title"] + " on {branch}"
    doc["parent"]["title"] = "Epic {branch}"
    return "---\nname: big\nvars:\n  branch: {required: true}\n---\n```aq-graph\n" + yaml.safe_dump(doc) + "```\n"


@pytest.fixture
async def handler(db, tmp_path):
    vault = tmp_path / "vault"
    (vault / "formulas").mkdir(parents=True)
    (vault / "formulas" / "big.md").write_text(_formula_text(PLAN_NODES))
    reg = FormulaRegistry()
    assert load_from_vault(reg, str(vault)) == []
    orch = MagicMock()
    orch.db = db
    orch.bus.emit = AsyncMock()
    orch._emit_notify = AsyncMock()
    orch.formula_registry = reg
    config = MagicMock()
    config.vault_root = str(vault)
    h = CommandHandler(orch, config)
    h._active_project_id = None
    return h


async def test_formula_cook_budget(handler, db):
    await seed_scale(db)
    async with count_statements(db) as c:
        started = time.perf_counter()
        res = await handler._cmd_formula_cook({"name": "big", "project_id": PROJECT_ID,
                                               "vars": {"branch": "b"}})
        elapsed = time.perf_counter() - started
    assert res["success"], res
    budget = 3 * PLAN_NODES + 23
    print(f"\nformula_cook({PLAN_NODES}) : {c['n']} statements, {elapsed:.2f}s (budget {budget})")
    assert c["n"] <= budget and elapsed <= 4.0


async def test_formula_show_is_read_only(handler, db):
    writes = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement[:80])

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with count_statements(db) as c:
            res = await handler._cmd_formula_show({"name": "big", "project_id": PROJECT_ID,
                                                   "vars": {"branch": "b"}})
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)
    assert res["success"], res
    assert writes == [] and c["n"] <= 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_formula_surface.py tests/perf/test_formula_statements.py -v`
Expected: FAIL — definitions/CLI/models missing.

- [ ] **Step 3: Implement**

Definitions (copy `create_task_graph`'s block as the template), category `formula` in `_TOOL_CATEGORIES`, `CATEGORY_CLI_NAMES["formula"] = "formula"` + description; response models; scope; `src/cli/formulas.py` with a `formula` Click group registered in `app.py` before `register_auto_commands()`, `show`/`cook` hand-crafted (parse `--var k=v` into a dict; reject entries without `=`), `list` left to auto-generation; formatters. Docs: `docs/specs/design/formulas.md` sections — 1 Purpose, 2 File format (frontmatter keys, one block, vars declared vs supplied), 3 Resolution order, 4 Provenance (the five metadata keys, snapshot row, label; `--as-cooked`), 5 Commands and CLI, 6 Doctor, 7 Limits (no control flow; single inheritance; strings only). Spec §18 P7 row → "Plan 3 — `src/task_graph/formulas.py`, `src/commands/formula_commands.py`, tests `tests/test_formulas_*.py`". CLAUDE.md bullet: ``- **Formulas:** `src/task_graph/formulas.py` (registry, `extends` merge, vars), `src/commands/formula_commands.py` (`formula_list|show|cook`), provenance in `creator.write_plan`. Files: `vault/[projects/<pid>/]formulas/<name>.md` (frontmatter + one `aq-graph` block). Spec: design spec Part III.``

- [ ] **Step 4: Verify**

Run: `pytest tests/test_formula_surface.py tests/perf/test_formula_statements.py tests/test_command_surface.py tests/test_response_model_registry.py tests/test_tool_registry.py tests/test_docs_sync.py tests/test_api_scope.py tests/test_cli_auto_commands.py -v -n auto`; `ruff check src tests`; then the full suite; `aq formula --help` via `python -c "from click.testing import CliRunner; from src.cli.main import cli; print(CliRunner().invoke(cli, ['formula','--help']).output)"`.

- [ ] **Step 5: Commit**

```bash
git add src/tools/definitions.py src/api/models/task.py src/api/scope.py src/cli/formulas.py src/cli/app.py src/cli/auto_commands.py src/cli/formatter_registry.py src/cli/formatters.py docs/specs/design/formulas.md docs/superpowers/specs/2026-08-28-swarm-work-model-design.md CLAUDE.md profile.md src/skills/aq-tasks/SKILL.md tests/test_formula_surface.py tests/perf/test_formula_statements.py tests/test_api_scope.py tests/test_command_surface.py tests/test_tool_registry.py
git commit -m "feat(formulas): tool defs, response models, aq formula CLI, agent scope, perf budget, docs and crosswalk

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage (§13 + formula rows of §14/§16/§17):** file location + shadowing, frontmatter (`name`, `description`, `vars` with required/default/enum, `extends` single inheritance), one `aq-graph` block, `vars` in body forbidden → Task 1. Resolution order (chain root-first with cycle detection → merge → var validation → defaults → `substitute_vars` → `validate_graph` → `create_graph`) → Tasks 2 and 4. Registry loaded/reloaded by the vault watcher, parse errors logged + `aq doctor formulas.parse` → Tasks 1 and 4. Commands `formula_list` / `formula_show` (incl. `as_cooked`, no writes) / `formula_cook` (`parent_id`, `dry_run`, one transaction with provenance) → Tasks 3 and 4. Provenance keys, snapshot context row, `formula:<name>` label → Task 3. CLI forms → Task 5. §14 response model `FormulaShow` and agent scope (list/show yes, cook no) → Task 5. §16 unit tests (merge, var validation, shadowing, cycles), invariant tests (event registered, tool defs) → Tasks 2, 3, 5. §17: independently mergeable, no migration.

**Placeholder scan:** every step has code or an exact instruction; the "confirm the real name" notes name the file and the fallback.

**Type consistency:** `Formula`/`VarDecl`/`FormulaRegistry`/`FormulaError` (Task 1) are consumed unchanged by Tasks 2, 4, 5; `ResolvedFormula(leaf, chain, vars, document, chain_sha, findings)` (Task 2) is what Task 4's `_resolved_graph` reads; `FormulaProvenance(name, scope, path, vars, chain_sha, snapshot)` and `write_plan(..., provenance=)` / `create_graph(..., provenance=)` (Task 3) are what Task 4 passes; the command response keys asserted in Task 4's tests are the ones Task 5's response models declare.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-29-swarm-formulas.md`. Per the standing instruction, execution proceeds with **superpowers:subagent-driven-development** on branch `swarm/hierarchy` in this worktree after Plan 2's final review closes.
