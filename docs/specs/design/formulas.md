---
tags: [design, formulas, task-graph, vault, swarm-work-model]
---

# Formulas — Reusable Task-Graph Templates

**Status:** Implemented (swarm-work-model §13, Plan 3)
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #9 simple interfaces)
**Related:** [[work-graph]], `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` §13 and §18 (P7)

---

## 1. Purpose

A formula is a reusable [[work-graph|task graph]] template — a parameterised
`aq-graph` document authored once in the vault and *cooked* (resolved,
substituted, validated, and created) as many times as needed, each time with
different variable values. It is the Agent Queue answer to "workflows as
data" (parity property P7): instead of an agent re-typing the same
review→fix→verify graph for every branch, an operator writes it once as
`vault/formulas/review-and-fix.md` and any caller — human, CLI, or another
agent via `formula_cook` — instantiates it with `--var branch=feat/x`.

Formulas compose through single inheritance (`extends`): a project can
shadow or specialise a system formula by name, and a formula can extend
another to reuse a common skeleton (e.g. a project-specific "hotfix" formula
extending a system "review-and-fix" base).

---

## 2. File Format

A formula file lives at:

- `vault/formulas/<name>.md` — system scope
- `vault/projects/<pid>/formulas/<name>.md` — project scope, **shadows** the
  system formula of the same name for that project (same shadowing rule as
  profiles and MCP servers)

Structure: YAML frontmatter, then exactly one fenced ` ```aq-graph ` block
(the same fence language [[work-graph|task-graph documents]] use elsewhere —
`create_task_graph --graph`, spec-embedded graphs).

Frontmatter keys:

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Formula id. Must match the filename stem. |
| `description` | no | One-line summary shown in `formula list`. |
| `vars` | no | Map of var name → `{required, default, enum}`. All three are optional; `required: true` with no `default` means the caller must supply it. |
| `extends` | no | Name of a single parent formula to inherit from (same scope-lookup rule as the leaf — project shadows system at *every* hop, not just the leaf). |

`vars` are declared in frontmatter only — a `vars:` key inside the
`aq-graph` body itself is not a declaration; the body may only *reference*
`{var}` placeholders inside string fields (titles, descriptions, etc. —
substitution is `src/task_graph/validator.py::substitute_vars`, the same
substitution `create_task_graph` uses for any graph with a `vars` block).

## 3. Resolution Order

`resolve_formula` (`src/task_graph/formulas.py`) runs, in order:

1. **`resolve_chain`** — walk `extends` from the leaf to the root,
   looking up each hop in the same scope (project falls back to system).
   Raises `formula.extends_missing` (named parent not found) or
   `formula.extends_cycle` (a formula extends one of its own ancestors).
   The chain is returned root-first.
2. **`merge_documents`** — fold the chain's raw authored documents
   (never `TaskGraph.to_dict()` defaults — only what each file's author
   actually wrote) into one document: `defaults` merged key-wise (child
   wins), `parent` merged field-wise, `nodes` merged by `key` (a child node
   replaces the fields it authors on the same-keyed parent node; `needs`,
   `labels`, `acceptance`, `context` are replaced wholesale when authored,
   never concatenated).
3. **`validate_vars`** — declared vars (`merged_var_decls`, child
   redeclaration wins) against supplied values: `formula.var_required`,
   `formula.var_enum`, `formula.var_unknown`. These are returned as
   findings, not raised — a caller reports them without losing the rest of
   the resolution.
4. **`apply_defaults`** — effective values: supplied (coerced to `str`)
   over declared defaults; only declared names appear in the result.
5. **`substitute_vars`** — the merged document's string fields get
   `{var}` substitution (`src/task_graph/validator.py`, shared with
   `create_task_graph`).
6. **`validate_graph`** — the same structural/semantic validation
   `create_task_graph` runs (profile existence, dependency shape, cycle
   detection, ...).
7. **`create_graph`** (cook only) — `build_plan` / `write_plan` in one
   transaction, with a `FormulaProvenance` attached.

`formula_show` stops after step 6 (or step 3, if var validation already
failed) — it never writes. `formula_cook` continues to step 7.

## 4. Provenance

Every non-dry-run `formula_cook` stamps five `task_metadata` keys on the
container task (`FormulaProvenance.metadata()`,
`src/task_graph/creator.py`):

- `formula` — the leaf formula's name
- `formula_scope` — `"system"` or `"project:<pid>"`
- `formula_path` — vault-relative path to the leaf file
- `formula_vars` — JSON of the effective (supplied + defaulted) vars
- `formula_chain_sha` — `chain_sha`: sha256 over the chain's per-file
  content hashes, root to leaf; changes whenever any file in the chain
  changes

A `formula_snapshot` `task_context` row is also appended, holding
`{cooked_at, chain_sha, document}` — the fully resolved, substituted,
validated document exactly as it was cooked. A container cooked more than
once keeps **every** snapshot row (one per cook); `formula_show
--as-cooked` picks the newest by `(cooked_at, chain_sha, id)` since
`task_context` has no timestamp column of its own and `id` is random hex.
The container also gets a `formula:<name>` label.

`--as-cooked <container_id>` on `formula_show` renders the snapshot exactly
as recorded — no registry lookup, no re-validation — so it reflects what
was *actually* cooked even if the vault file has since changed.

## 5. Commands and CLI

Three commands (`src/commands/formula_commands.py`):

- **`formula_list`** — enumerate formulas visible to a project (system plus
  project overrides, shadowed by name). Read-only. Agent-scoped.
- **`formula_show`** — resolve and validate, never writes. Agent-scoped.
- **`formula_cook`** — resolve, validate, and create the graph in one
  transaction. **Not** agent-scoped — a non-elevated session gets
  `"formula_cook is not available to agent sessions"`.

CLI (`src/cli/formulas.py`; `list` is auto-generated, `show`/`cook` are
hand-crafted for repeatable `--var`):

```
aq formula list [--project-id PID]
aq formula show <name> [-p PID] [--var k=v]...
aq formula show --as-cooked <container_id>
aq formula cook <name> -p PID [--var k=v]... [--parent TASK_ID] [--dry-run]
```

`--var` is repeatable (`--var branch=feat/x --var fixer=coding`); an entry
without `=` is a CLI usage error, not a silently-dropped var.

## 6. Doctor

`aq doctor` runs `formulas.parse` (`src/doctor/formula_checks.py`): `OK`
when every formula file in `registry.errors` parsed cleanly, `WARN` with
the failing file list otherwise. Report-only — a malformed formula is fixed
by editing the vault file, not by `aq doctor --fix` (the vault watcher
re-parses on save and leaves the previous good entry live until then, so a
half-edited file never takes a formula offline mid-edit).

## 7. Limits

- **No control flow.** A formula is a static document, not a script —
  no conditionals, loops, or computed node counts. Branching workflows are
  expressed as `extends` variants (a "hotfix" formula extending "review",
  say), not as an `if` inside one file.
- **Single inheritance.** `extends` is one name, not a list — a formula has
  at most one parent. Composition beyond a linear chain is out of scope;
  write the shared skeleton once and extend it, rather than mixing in
  multiple parents.
- **Strings only.** Vars substitute into string fields via `{var}`
  placeholders (titles, descriptions, ...); there is no typed var (int,
  bool, list) and no expression language — `apply_defaults` coerces
  everything to `str`.
