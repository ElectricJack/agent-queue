# Smart test selection — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [managed long-running commands](2026-09-24-managed-long-running-commands.md) ·
[exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[resource-aware planner](2026-09-24-resource-aware-planner.md) ·
[adversarial review recipe](2026-09-24-adversarial-review-recipe.md) ·
existing: [CI main sentinel](2026-09-05-ci-main-sentinel-design.md),
[CI sentinel repair coverage](2026-09-16-ci-sentinel-repair-coverage-design.md),
[intelligence class simplification](2026-09-12-intelligence-class-simplification-design.md),
[resource gating guide](../../guides/resource-gating.md),
[development integration guide](../../guides/development-integration.md)

## 1. The ask

"Smart test selection: give Jev the change set plus the list of test areas and
let it pick what to run. Cheap call, attacks the 20-minute full run."

Read here as: before running tests for a change, make one cheap LLM call —
inputs are the diff (or its summary) and a catalogue of test areas; output is
the set of test files to run, with reasons. The goal is to replace both the
20-minute full run and an expensive worker model's ad-hoc guess at "the related
area suite" with a fast, recorded, measurable selection.

**Interpretation flag.** "Jev" appears nowhere in the repository. This spec
assumes it means a cheap/fast model reached through the LLM direct path with a
`fast-*` intelligence class (`fast-low` / `fast-high`). See open question 1.

## 2. What exists today

**Test layout.** 657 top-level `tests/test_<area>.py` modules plus
`tests/perf/`, `tests/llm/`, `tests/task_graph/`, `tests/fixtures/`; ~13,950
tests (13,816 after default deselects, per `CLAUDE.md`) — about 21 tests per
module. `pyproject.toml` sets `testpaths = ["tests"]` and
`addopts = ... -m "not perf and not migration and not slow and not tmux and not integration"`
with `--durations=10`. `aq test` always adds `--dist loadfile`, so **a module is
already the natural unit of scheduling** (per-module DB fixtures). The dashboard
has a separate Vitest suite (`dashboard/package.json`: `"test": "vitest run"`).

**Import fan-out (measured with grep, top-level imports only).** Of the 657
modules: `src.database` is imported by 276, `src.models` by 274, `src.config`
by 199, `src.orchestrator` by 123, `src.commands.handler` by 106, `src.cli` by
63. `tests/conftest.py` itself imports `src.models` and `src.config`. 38
modules import nothing from `src` at all, and 49 scan the tree textually
(`rglob`, `Path("src")`, …) — ratchets such as `tests/test_v1_removal.py`,
`tests/test_sqlite_removal.py`, `tests/test_cli_module_map.py`,
`tests/test_command_surface.py`. Many production imports are function-local.
Implication: an import graph is precise for leaf modules and degenerate for
hubs, and it cannot see the ratchets at all.

**No test-impact tooling exists.** No `pytest-testmon`, no `.coveragerc`, no
coverage configuration; `pytest-cov>=6.0` is in the `dev` extra but unused by
any script or workflow found. Nothing named "affected" / "impacted".

**`aq test`** — `src/cli/test_runner.py`. `test_command` (line 692) is a
pass-through wrapper; `_is_full_suite` (line 395) is a *static* classifier:
selecting ≥ `_FULL_SUITE_SHARE = 0.5` of modules makes a run "full" and takes
the box-wide full-suite lock (`full_suite_lock_dir`,
`src/resources/semaphore.py:72`). A narrowing `-k`/`-m` or `--lf` with failures
on record makes a run a slice (`_last_failed_is_a_slice`, line 382).
`_missing_paths` (line 260) refuses non-existent path arguments before taking
a slot.

**Who selects tests today.** The worker profile says "Run focused tests using
the project's resource controls (`aq test`), then the related area suite"
(`src/profiles/defaults/worker-claude/profile.md:143`, same in
`worker-codex`). So selection already happens, by the worker's own (expensive)
model, unrecorded and without a catalogue. The `pr-merger` profile says the
same ("focused and area tests only").

**The development publisher's "selected validation".**
`DevelopmentPolicy` (`src/integration/development.py:143`) holds a fixed
`commands` list; `DevelopmentIntegration.validate` (line 414) runs each via
`development_validation.run_check`. This repo's own project is configured with
one fixed command, `isolated-tests.py tests/test_development_integration.py`
(`docs/guides/development-integration.md`). Its evidence already records
`failing_tests` (`parse_pytest_output`, `development_validation.py:122`),
counts, durations and a classification. That is a second consumer: a batch's
diff could choose its own validation.

**CI.** `.github/workflows/tests.yml` runs on `push` to `main`, `aq/parent/**`,
`aq/integration/**` (no `pull_request` trigger). Matrix arms: `cli-conformance`
(`aq test tests/test_cli_inventory.py tests/test_cli_conformance.py`),
`default` (`pytest tests/ -n auto --dist loadfile`), `migration-and-slow`,
`postgres-integration`; `timeout-minutes: 30`. An `integration-attestation` job
lets `main` reuse an integration candidate's exact-SHA evidence instead of
re-running. `macos-acceptance.yml` is separate.

**CI main sentinel.** `_observe_ci_baseline` (`src/commands/ci_commands.py:298`)
reads failing check runs and, per failed job, the failing pytest node ids via
`GitManager.ajob_failed_tests` (`src/git/manager.py:4782`); `failure_signature`
(line 44) keys repairs by the failing set. This gives us historical
"which tests failed on which commit" data — the ground truth for measuring a
selector's recall.

**Known-failing baseline.** Agents compare against the vault note
`projects/agent-queue/notes/full-suite-baseline-<date>.md` (operator vault,
not in the repo). A selector must not treat those as change-caused.

**LLM direct path.** `LLMClient.complete(messages, *, system, spec)`
(`src/llm/client.py:271`) with `LLMCallSpec(intelligence_class=..., caller=...,
max_tokens=...)` (`src/llm/spec.py:16`). The client is "owned by the
orchestrator; consumers receive it, never build one" — consumers today are the
playbook LLM executor (`src/playbooks/executors/llm.py:476`),
`Orchestrator` (`core.py:700`), `vault_index.py`, `reference_stub_enricher.py`.
Calls are logged by `LLMLogger` under `<data_dir>/logs/llm/`. The client
honours provider availability (`_block_reason` / `_resolve_gated`). `fast-low`
resolves to `claude-sonnet-5` (thinking low) / `gpt-5.6-luna` /
`gemini-2.5-flash` (`src/prompts/default_intelligence_classes/fast-low.md`);
`fast-high` is the same models at `xhigh`. A second, key-less path exists:
`src/llm/cli.py` (one-shot, tool-free calls through logged-in agent CLIs).
**Consequence:** `aq test` runs as a CLI process in a worktree and cannot hold
an `LLMClient`; a selection call has to go through a daemon command (the CLI
reaches it via `AQ_API_URL`) or through the CLI path.

## 3. Gaps

1. No catalogue of test areas: 657 file names are the only index; nothing says
   what `tests/test_settling.py` covers.
2. No mapping from source to tests — static, dynamic or curated.
3. No recorded selection: when a worker runs "the area suite" nobody can later
   ask whether it would have caught what CI caught.
4. No measurement: we do not know today's recall of worker-chosen tests, so
   we cannot say whether a selector is better or worse.
5. Non-Python changes (profiles, playbooks, markdown defaults under
   `src/prompts/`, `openapi.json`, alembic revisions, `dashboard/`) have no
   natural mapping, yet ratchet tests exist precisely for them.
6. The full-suite lock is keyed on *breadth*; an LLM that picks ~330+ modules
   silently becomes a full-suite run and queues behind the lock.

## 4. Implementation options

### Option A — LLM picks from an area catalogue given the diff

Sketch. A generated catalogue — one line per test module: path, first
docstring line, the `src` modules it imports most, markers used — about 657
lines, ~15–25k tokens. Prompt: the catalogue, `git diff --stat`, the changed
file list, changed symbol names (cheap AST diff), and truncated hunks up to a
budget. Output: strict JSON `{files:[{path, reason, confidence}], run_full:
bool, why}` validated against the catalogue (unknown paths dropped, logged).
One `fast-low` call per selection; cached by `(diff hash, catalogue hash)`.

Touches: catalogue generator (script + committed or cached artifact), a daemon
command `test_select` (contract, handler mixin, CLI auto-derived), `aq test
--aq-smart` in `test_runner.py`, a selection record (task comment or metadata).
Pros: handles non-Python changes and "semantic" coupling; cheap; tiny code.
Cons: nondeterministic; recall unknown until measured; susceptible to omitting
the hub-change blast radius; the model sees only what fits in the budget.
**Size: M.**

### Option B — static import-graph mapping

Sketch. Parse `src/` and `tests/` with `ast` (all `Import`/`ImportFrom` nodes,
including function-local ones), build module → test-module reverse transitive
closure, select every test module whose closure contains a changed module.
Add path rules for non-Python inputs (e.g. `src/prompts/default_playbooks/** →
tests/test_default_playbook_v2_artifacts.py`) and an always-run list for
source-scanning ratchets.
Pros: deterministic, free, explainable, no model outage dependency.
Cons: hub changes (`src/database/`, `src/models.py`, `src/config.py`) select
40%+ of the suite, so the saving vanishes exactly where risk is highest;
dynamic dispatch (command registry, plugin loader, `importlib`) is invisible;
rules for non-Python files must be maintained by hand. **Size: M.**

### Option C — coverage-based test impact (testmon-style)

Sketch. A periodic instrumented full run (nightly, or the CI `default` arm with
`--cov` contexts per test) records which source files — or lines — each test
module executes; store the map keyed by commit. Selection = test modules
whose recorded coverage intersects the diff's changed files/lines, plus
everything new or changed in `tests/`.
Pros: the most accurate signal for Python changes, including dynamic dispatch.
Cons: coverage overhead under xdist; map staleness between refreshes; blind to
files read as data (markdown defaults, playbooks, JSON) unless tracing is
extended; a large artefact to store and ship to worktrees; `pytest-testmon`
itself keeps a local SQLite DB per checkout, which does not fit 8 worktree
slots sharing a box. **Size: L.**

### Option D — hybrid with a safety net

Sketch. A deterministic tier (B, later C) produces a **floor** that the model
cannot remove; the LLM (A) may **add** modules (non-Python changes, semantic
neighbours), may flag `run_full`, and, when the floor is huge (hub change),
may **rank** it so the likely-relevant modules run first with `-x`-style early
feedback — but not drop it. The full suite still runs in CI (unchanged) and a
selector-miss is detected there and fed back as a measurement.
Pros: bounded worst case; the model is used where static analysis is weakest.
Cons: two mechanisms to maintain; saving on hub changes is limited to ordering.
**Size: L** (M for the first cut: A + a thin B floor).

## 5. Initial take

*Provisional.* Start with **Option A in shadow mode, plus a thin deterministic
floor** (D's first cut), and let measurement decide whether C is worth it.

- **Floor:** changed or added `tests/` files; `tests/test_<stem>*.py` for each
  changed `src/**/<stem>.py`; an explicit always-run list for ratchets
  (maintained in one file, checked by a test that every module which globs the
  source tree is on it); the `cli-conformance` pair whenever `src/cli/` or a
  command contract changes.
- **Where it runs, in order:** (1) `aq test --aq-smart [--aq-base <ref>]` —
  computes the diff against the merge base (default `origin/main`), calls
  `test_select` over the API, prints the selection with reasons, records it,
  then runs it through the normal slot path (and therefore the normal
  full-suite lock if the selection is broad); falls back to the floor alone
  when the daemon or model is unavailable, and says so. (2) Worker profile
  guidance changes from "the related area suite" to `aq test --aq-smart`.
  (3) Later, an opt-in `DevelopmentPolicy.validation = "smart"` for the
  publisher, selecting per batch diff. (4) CI stays full; CI is the safety net
  and the measurement source, not a consumer.
- **Measure before trusting:** for every red CI run the sentinel already
  observes, replay the selector on the commit's diff (against its parent) and
  record module-level recall = |failing modules selected| / |failing modules|,
  excluding known-baseline failures. Also sample green commits for selection
  size / time saved (per-module durations from a periodic `--durations=0` or
  junit run). A target such as ≥ 0.98 module recall over 30 red runs before
  making `--aq-smart` the profile default — the number is a placeholder.
- **Cost:** one `fast-low` call, ~20–30k input tokens and < 1k output, per
  selection; cached per diff. Latency seconds, against a 20-minute run.

## 6. Open questions

1. **Is "Jev" a `fast-*` intelligence class, a specific model, or a named
   agent/profile?** Decides whether this is a direct-path LLM call (this spec)
   or a short worker task/session with tools (it could then read files itself).
2. **Granularity: modules or node ids?** Modules match `--dist loadfile` and
   per-module DB fixtures; node ids save more but make the catalogue ~20×
   larger and selection brittle.
3. **Who is authoritative — may a close pass on a smart selection alone?**
   Today "focused + area" is enough to close. If smart selection replaces it,
   is a later CI miss a worker failure, a selector failure, or neither?
4. **Where is selection recorded?** Task metadata (`test_selection`), a task
   comment, or a new table joined later to CI outcomes for recall? Needed for
   Q-measurement to be automatic rather than a one-off study.
5. **Catalogue source.** Generated from docstrings and imports (drifts
   silently if docstrings are poor), or a curated `tests/AREAS.md`-style map
   with a ratchet test? Who maintains it?
6. **How is the diff presented within budget?** Full hunks, names only, or an
   AST-level "changed symbols" summary? Big diffs (migrations, generated
   `packages/aq-client/`, `openapi.json`) must be summarised, not pasted.
7. **Should the LLM ever remove floor modules?** §5 says no. Is there a case
   (hub change) where ranking + a time budget is acceptable instead?
8. **Interaction with the full-suite lock.** A broad selection becomes "full"
   under `_is_full_suite`. Should `--aq-smart` cap selection size, or accept
   the lock, or switch to `run_full` explicitly?
9. **Provider outage behaviour.** `LLMClient` honours provider availability; is
   floor-only the right degraded mode, or should the call fall back across
   providers / to the CLI path (`src/llm/cli.py`)?
10. **Security of the input.** The diff is worker-authored; a prompt-injected
    comment could ask the selector to skip tests. The floor and CI bound the
    damage — is that sufficient, or should the selector see names only?
11. **Dashboard (Vitest) and non-pytest checks.** In scope for the first cut,
    or pytest only?
12. **CI use.** Should integration-branch CI (`aq/integration/**`) run a smart
    subset first for fast feedback while the full arm continues? Changes
    `.github/workflows/tests.yml` and the attestation logic that lets `main`
    reuse candidate evidence — high blast radius.
13. **Determinism/caching.** Same diff, same selection? Cache by diff hash, or
    set temperature/seed where the provider allows, or accept variance?

## 7. Dependencies and sequencing

- **Independent first steps (S each):** the catalogue generator; the floor
  (pure function of a file list, unit-testable); an offline recall harness
  that replays selection over past sentinel-observed red commits.
- **Then (M):** `test_select` daemon command + `aq test --aq-smart`, shadow
  mode — selection is printed and recorded, the worker still runs its own
  choice; compare.
- **Then:** profile guidance change (requires `aq agent profile-reseed` for
  existing vaults, since `ensure_default_profiles` is write-if-absent), and
  optionally the publisher's `smart` validation mode.
- **Optional (L):** coverage map (Option C) if measured recall on hub changes
  is poor.
- Pairs with [managed long-running commands](2026-09-24-managed-long-running-commands.md)
  (a selected run is a natural managed run whose failures feed back on wake)
  and reduces load for the [exclusive job queue](2026-09-24-exclusive-job-queue.md)
  and [resource-aware planner](2026-09-24-resource-aware-planner.md).

## 8. Non-goals

- Removing or shrinking the full suite in CI on `main`.
- Test flakiness detection or quarantine.
- Rewriting tests to be more selectable (e.g. splitting hub fixtures).
- Choosing the markers (`perf`, `migration`, `slow`, `tmux`, `integration`):
  those deselects stay as `aq test` applies them today.
- Selecting tests for other projects' repositories in the first cut (the
  catalogue and floor rules are specific to this repo's layout).
