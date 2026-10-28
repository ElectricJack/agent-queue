---
title: Narrow Bazel test inputs per module
date: 2026-10-27
task: bold-rapids-90
status: plan
---

# Narrow Bazel test inputs per module

Goal: a change selects only the Bazel test targets it can affect. Today, replaying three recorded
commits as static selections against `tools/bazel/test_suites.json` (38,517 nodes, 6,421 targets)
skips 0.43% of nodes for a small fix (`504810ddc`), 0% for a cross-cutting change (`469774ea8`) and
0% for a docs-only change (`1ec531c8b`). The task brief measured 0.44% / 0% / 0%. The replay script is
`~/.agent-queue/vault/projects/agent-queue/plans/2026-10-27-bazel-narrow-test-inputs/scenario_replay.py`
(vault only). It reads just the committed manifest: `--base current` reproduces today and
`--base split` applies S4.

This plan covers four things: why the inputs are broad, the shared mechanism change (one child,
**S**), 18 module batches that depend only on S, and a final verification child, **E**.

## Findings

Static analysis covered all 1,014 catalogue modules. It used the real `DependencyAnalyzer` from
`scripts/bazel/test_dependencies.py` with scratch patches and ran nothing from the suite. A
whole-repository discovery takes about 5 minutes and about 3 GB of RSS under `nice`.

**F1. Two plugin loaders make 716 of 725 fine-grained modules conservative.**
- `src/plugins/internal/__init__.py` loads plugins with `pkgutil` and `import_module`.
- `src/plugins/loader.py:523` loads operator plugins with `spec_from_file_location`.
- Almost every closure reaches both. The analyzer records "unresolved dynamic import" or "unresolved
  file import" for them and falls back to every src file plus `docs/**`.
- A source declaration resolves both:
  - the internal loader's imports are the glob `src/plugins/internal/*.py` plus package inits;
  - operator plugins live outside the repository, so that loader needs no import.
- With both resolved, 54 modules stay conservative instead of 1,004. Each remaining reason is in the
  table.

**F2. False-positive `docs/` literals in src force the documentation tree into 960 modules.**
- Twelve src files contain a literal `"docs"` or `docs/...`, which the analyzer treats as reading
  `docs/`. The files:
  - `src/cli/styles.py`
  - `src/commands/playbook_v2_commands.py`
  - `src/config.py`
  - `src/doctor/dashboard_server_checks.py`
  - `src/install/onboarding.py`
  - `src/install/platform.py`
  - `src/integration/bookkeeping_guard.py`
  - `src/models.py`
  - `src/plugins/internal/files.py`
  - `src/portable_config.py`
  - `src/tools/definitions.py`
  - `src/workspace_spec_watcher.py`
- None of them is a test-time docs read. They are emoji-map keys, enum values, config defaults,
  message text and doctests.
- About 64 tests really read docs, plus the helpers `tests/integration_ownership.py` and
  `tests/selection_fixture_repo.py`.
  - `selection_fixture_repo.py` only writes docs paths into a temporary fixture repository.
  - `integration_ownership.py` matches `.md` suffixes under `src/`.
  - Both are false positives as well.
- Separately, the root glob `*` in `python_repository_data` puts `AGENTS.md`, `README.md`,
  `profile.md`, `CLAUDE.md` and `LICENSE` into every target. A docs-only change touching `AGENTS.md`
  therefore selects everything.

**F3. The src import closure is one strongly connected component of 707 files.**
- Lazy function-level imports hold it together.
  - The median module closure is 1,129 of 1,224 src files.
  - No single edge cut breaks it.
- Definition-level reachability brings about 25% of modules down to roughly 190 src files. The
  prototype treats a src file the way the analyzer already treats test support files: it activates
  only the top-level definitions a reached name refers to.
- The rest still reach about 1,080 src files through the `PostgreSQLDatabaseAdapter` facade:
  - `src/database/adapters/postgresql.py` imports about 60 query mixins at top level;
  - `src.database.Database` is a lazy alias for it;
  - a class is activated whole.
- Member-level (method-granular) activation was tried as a stand-alone estimate and gives no
  further gain. All 60 sampled modules reach 1,015 or more src files. Attribute names and string
  constants activate same-named methods across the adapter and `CommandHandler` mixins.
- Splitting that facade is a code refactor and is outside this plan. See **Follow-ups**.

**F4. The generated Bazel outputs and declarations conflict in parallel work.**
- The generated files are `tools/bazel/test_suites.json` (22 MB), 65 `BUILD.bazel` files and
  `tools/bazel/python_data.bzl`.
- None of them is in `.gitattributes` `merge=aq-generated` or in `scripts/regenerate-generated.sh`.
- `scripts/bazel/test_dependency_inputs.json` (127 entries) is one sorted dictionary. Batches grouped
  by area interleave alphabetically, for example `test_integration_*` across B01 and B02, so adjacent
  edits conflict.
- Fine-grained opt-in lives only in the generated manifest (`fine_grained_modules`, written by
  `generate_targets.py --area`). Regenerating "either side" on a conflict would drop the other
  side's opt-ins.

## Predicted skip percentages

These are static replays: a module is selected when any of the following holds.
- It is conservative.
- The changed path is the module itself.
- The changed path is in the new runtime base data: root non-documentation files, `.github/`,
  `scripts/`, `migrations/`, `packages/aq-client/`, `tools/bazel/`.
- The changed path is in the module's `source_files`, `test_sources`, `other_sources` or `data_files`.
- The changed path is under `docs/` or is a root `*.md`/`LICENSE`, and the module keeps a
  documentation input.

The prototype rows use a scratch analyzer patch, so they are predictions. After the batches land, E
measures the real numbers with `scenario_replay.py --base split`.

| variant | small 504810ddc | cross 469774ea8 | docs-only 1ec531c8b |
|---|---|---|---|
| today (committed manifest) | 0.43% | 0.00% | 0.00% |
| S1 plugin declarations + S3 fine-grained default, docs flag kept | 0.47% | 0.00% | 0.00% |
| S1 + S4 + test-side docs flag (S1 docs override) | 0.47% | 0.47% | 70.82% |
| + S6 definition-level src reachability | **18.52%** | **0.53%** | **70.82%** |
| + batches replace docs flags with exact docs files | 18.52% | 0.60% | **78.17%** |

What each scenario shows:
- **Small fix.** It changes `src/doctor/db_checks.py`, which 743 modules reach. Most of them reach it
  through the database and command facades.
- **Cross-cutting change.** It touches `src/main.py`, `src/sessions/tmux.py`, `src/sessions/env.py`
  and `src/env_scrub.py`, which nearly every closure reaches. This plan cannot move it much; the
  facade follow-up can.
- **Docs-only change.** About 70–78% is the expected range.
  - The 112 modules still selected are the 54 conservative modules plus genuine docs directory
    readers.
  - The `dir?` modules in the table are reviewed by their batch, so the number may rise further.

## Shared mechanism: child S

One fast-high-claude refactor child. Every batch depends on it. All of S lives in
`scripts/bazel/test_dependencies.py`, `scripts/bazel/generate_targets.py` and the declaration
sources, plus their tests. S does not hand-tune any module.

**S1. Source declarations for src files.**
- Declarations can be keyed by a src path, not only by a test module.
- Each one carries:
  - `resolves`: the exact conservative reasons it answers;
  - `imports` or `import_globs`: what the loader can load;
  - optional `data`.
- A declared reason is removed from every closure that reaches the file. The globbed imports are
  added instead.
- A `resolves` entry that no longer matches a recorded reason stops generation as stale.
- Ship the two plugin-loader declarations from F1.
- Add `docs: false` on a src or test path. It drops that file's docs-literal flag (the F2 false
  positives) and must name the reason.
- Make the docs flag test-side by default: a src literal sets docs only when the src file is
  declared a docs reader.

**S2. Per-module `resolves` and `broad`.**
- A module declaration may resolve its own recorded reasons with explicit imports or data, as in S1.
- Or it may set `broad: "<reason>"`. That keeps the module conservative and puts the reason in the
  manifest.
- Unknown keys stay errors.

**S3. Fine-grained by default.**
- Every catalogue module is fine-grained unless its declaration sets `mode: legacy` with a reason.
- Remove the manifest-only `fine_grained_modules` stickiness. Keep `--area` as a no-op alias, or
  retire it with its doc in `tools/bazel/PYTHON.md`.
- This flips the 289 legacy modules in S. The batches are what verify them under Bazel execution.

**S4. Split the base data.**
- `python_repository_data` keeps root non-documentation files, `.github/**`, `scripts/**`,
  `migrations/**`, `packages/aq-client/**` and `//tools/bazel:repository_inputs`.
- Root `*.md` and `LICENSE` move to `python_documentation_data`, next to `docs/**`. That group is
  opt-in through the docs flag or exact data files.
- Update the generated `python_data.bzl` template and `tools/bazel/README.md`/`PYTHON.md`.

**S5. Conflict-free parallel work.**
- Shard `scripts/bazel/test_dependency_inputs.json` into one file per key under
  `scripts/bazel/test_dependency_inputs/`, mirroring the path: for example
  `tests/test_x.py.json` and `src/plugins/loader.py.json`. Migrate the 127 entries and delete the old
  file.
- Register `tools/bazel/test_suites.json`, every generated `BUILD.bazel` section owner and
  `tools/bazel/python_data.bzl` as `merge=aq-generated` in `.gitattributes`.
- Add the Bazel regeneration (collect plus generate) to `scripts/regenerate-generated.sh`, so that
  `--check` and the integration train regenerate rather than merge.
  - If whole-repository collection is too slow for the train, S records that and makes
    `generate_targets.py` reuse the committed collection when test bytes did not change.

**S6. Definition-level reachability for src.**
- Apply the existing support-template mechanism to `src/` files as well as `tests/`:
  - module-level statements, decorators, default values and class bodies are startup;
  - top-level functions and classes activate by name;
  - decorated definitions stay startup, because registries depend on decoration side effects.
- Soundness guards:
  - A string constant equal to a top-level definition name activates that definition, which covers
    `getattr(module, "name")`.
  - `__getattr__` module hooks activate every import inside them.
  - `__all__` with `from x import *` activates the listed names.
- The prototype patch is two lines in `DependencyAnalyzer`, the `tests/` prefix check at line 796
  and the startup rule in `_support_template`. S hardens and tests it.
- S6 is the largest risk and the largest win. Under-inclusion surfaces as an ImportError under Bazel
  execution in the batches, not silently.

**S acceptance:**
- `generate_targets.py --check` is clean.
- `aq test tests/test_bazel_manifest.py tests/test_bazel_test_dependencies.py tests/test_selection_catalogue.py`
  passes.
- New regressions cover:
  - plugin declarations;
  - a stale `resolves` refusal;
  - `docs: false`;
  - `broad`;
  - default fine-grained mode;
  - the S4 filegroups;
  - one definition-level case that excludes an unused lazy import;
  - one `getattr`-by-string case that keeps it.
- A merge-driver check: two branches that change different shards regenerate cleanly.
- The S child records the post-S static replay numbers in its close summary.

## Batching

There are 18 batches, B01–B18, of 52–62 modules each, grouped by the catalogue's primary area.

- **Why not 20–40 modules per batch:** the worker filing quota is 20 tasks per held task. The full
  set of 1,014 modules at about 40 per batch would need 26 batches plus S and E. Eighteen batches
  plus S and E use the quota exactly. Batches with mostly mechanical modules carry more.
- **Dependency edges:** each batch has exactly one `blocks` edge, on S. There are no edges between
  batches. E depends on all 18 batches.
- **Routing:** fast-high-claude, strict.

**Batch child procedure.**
- Regenerate after S.
- For each listed module:
  - read its manifest entry;
  - resolve each recorded conservative reason with a declaration, or mark the module `broad` with
    the table's reason;
  - replace a docs flag with exact docs files where the reads are literal;
  - run the module through `aq test` and `aq test --aq-backend bazel`, in groups of about 10;
  - turn every missing runfile into a declaration.
- Edit only your own modules' declaration shards, then regenerate. Never hand-merge generated
  outputs.

**Regeneration:**

```bash
python scripts/generate-selection-catalogue.py          # only if a test module moved or its src imports changed
python scripts/bazel/inventory.py collect --output .aq/tmp/collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/collection.json
python scripts/bazel/generate_targets.py --check
```

**Batch acceptance:**
- Every listed module is fine-grained, and resolved unless broad with a reason.
- `generate_targets.py --check` is clean.
- The modules pass under `aq test` and `aq test --aq-backend bazel`.
- `tests/test_bazel_manifest.py` passes.

## Verification: child E

One fast-high-claude test child that depends on all batches.
- It replays the three scenarios as static selections against the regenerated manifest:
  `504810ddc`, `469774ea8` and `1ec531c8b`. It uses
  `~/.agent-queue/vault/projects/agent-queue/plans/2026-10-27-bazel-narrow-test-inputs/scenario_replay.py --base split`
  and also runs `--base current` as the control.
- It reports skip percentages against 0.44% / 0% / 0%, and against this plan's predictions of
  18.5% / 0.5% / 70–78%.
- It lists the modules still conservative or broad with their reasons.
- It also runs a Bazel query proof that `rdeps` of each scenario's changed files equals the static
  selection.

## Child tasks

| Child | Task | Depends on |
|---|---|---|
| S: shared mechanism | `bold-rapids-90.1` | — |
| B01: integration trains | `bold-rapids-90.2` | `bold-rapids-90.1` |
| B02: integration promotion and ownership | `bold-rapids-90.3` | `bold-rapids-90.1` |
| B03: git, GitHub and delivery | `bold-rapids-90.4` | `bold-rapids-90.1` |
| B04: migrations | `bold-rapids-90.5` | `bold-rapids-90.1` |
| B05: database, claims and pools | `bold-rapids-90.6` | `bold-rapids-90.1` |
| B06: playbook engine and authoring | `bold-rapids-90.7` | `bold-rapids-90.1` |
| B07: shipped playbooks, profiles and classes | `bold-rapids-90.8` | `bold-rapids-90.1` |
| B08: knowledge, memory and vault | `bold-rapids-90.9` | `bold-rapids-90.1` |
| B09: task lifecycle and routing | `bold-rapids-90.10` | `bold-rapids-90.1` |
| B10: task graph, formulas and layout | `bold-rapids-90.11` | `bold-rapids-90.1` |
| B11: reviews, asks, escalations and digest | `bold-rapids-90.12` | `bold-rapids-90.1` |
| B12: messages, Discord and events | `bold-rapids-90.13` | `bold-rapids-90.1` |
| B13: sessions, tmux and transcripts | `bold-rapids-90.14` | `bold-rapids-90.1` |
| B14: agent waits, roster and workspaces | `bold-rapids-90.15` | `bold-rapids-90.1` |
| B15: providers, LLM client, metrics and orchestrator | `bold-rapids-90.16` | `bold-rapids-90.1` |
| B16: CLI, command dispatch, API and MCP tools | `bold-rapids-90.17` | `bold-rapids-90.1` |
| B17: config, install, doctor and projects | `bold-rapids-90.18` | `bold-rapids-90.1` |
| B18: dashboard, plugins, selection, Bazel, docs guards and e2e | `bold-rapids-90.19` | `bold-rapids-90.1` |
| E: verification | `bold-rapids-90.20` | every batch (`.2`–`.19`) |

Worker tokens cannot add dependency edges (`out_of_scope: add_dependency`), so the
supervisor was asked to add the 36 blocks edges in the table. Batches have no edges between them.

## Broad modules (kept conservative, with reason)

- e2e: `test_e2e_cli_stateful`, `test_e2e_kit_fixtures`, `test_e2e_probe`.
- Bazel graph and runtime: `test_bazel_manifest`, `test_bazel_test_dependencies`,
  `test_bazel_runtime`.
- Docs coverage: `test_documentation_coverage`, `test_documentation_checks`, `test_command_docs`.
- Whole-surface inventories:
  - `test_import_cycles`, `test_database_imports`;
  - `test_config_schema_inventory`, `test_generated_command_contracts`,
    `test_command_contracts_registry`;
  - `test_cli_startup_offline`.
- Build artifacts: `test_release_artifact`, `test_dashboard_server_bundle`.

The other 37 conservative modules after S1 load a known file or a test helper by path. Their batch
resolves each one with a declaration (see the class column).

## Follow-ups (not filed; filing quota)

- **Split the database and command facades.** Have callers import the query mixin they use, or make
  `PostgreSQLDatabaseAdapter` composition lazy. Do the same for `CommandHandler` mixins. This is the
  only lever left for the small-fix and cross-cutting scenarios once S6 lands.
- **Narrow `scripts/**` in the base data** to the scripts that tests execute. Today any change under
  `scripts/` selects everything.
- **A selection-soundness ratchet:** a CI job that runs the selected targets for real commits and
  compares the result with a full run.


## Per-module table

Columns: **current** is the mode in `tools/bazel/test_suites.json` today (`legacy` = package libraries, `conservative` = fine-grained but every src file because of a recorded reason, `resolved`). **today src** is the src file count today (legacy: package library count). **proposed** is the prototype result after S1–S6 (definition-level src files; test/support sources; data files; docs: `none` = no documentation input, `exact` = only the literal docs paths already in data, `dir?` = a test-side docs directory reader to confirm or narrow to a glob). **class** is narrow or broad-with-reason. Counts come from the scratch prototype and will move when S lands; the batch child records the real numbers.


### B01 — integration trains (54 modules)

Areas: integration-trains.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/perf/test_train_selection.py` | integration-trains | conservative | 1224 | src 1080, tests 14, data 866, docs dir? | narrow | low |
| `tests/test_attestation_check_run_id.py` | integration-trains | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_branch_sweep.py` | integration-trains | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_fast_smooth_integration_e2e.py` | integration-trains | conservative | 1224 | src 1080, tests 15, data 866, docs dir? | narrow | low |
| `tests/test_hosted_attestation.py` | integration-trains | conservative | 1224 | src 190, tests 5, data 156, docs none | narrow | medium |
| `tests/test_integration_attestation.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1038, docs exact | narrow | medium |
| `tests/test_integration_batches.py` | integration-trains | legacy | libs 58 | src 1080, tests 13, data 1041, docs dir? | narrow | medium |
| `tests/test_integration_candidate_baseline.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1036, docs exact | narrow | medium |
| `tests/test_integration_check_reconciliation.py` | integration-trains | legacy | libs 58 | src 1080, tests 8, data 1040, docs exact | narrow | medium |
| `tests/test_integration_checks.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1041, docs exact | narrow | medium |
| `tests/test_integration_ci.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1036, docs exact | narrow | medium |
| `tests/test_integration_ci_producers.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1043, docs exact | narrow | medium |
| `tests/test_integration_cleanup.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1039, docs exact | narrow | medium |
| `tests/test_integration_cli_controls_present.py` | integration-trains | legacy | libs 58 | src 1080, tests 5, data 1037, docs exact | narrow | medium |
| `tests/test_integration_close_merge.py` | integration-trains | legacy | libs 58 | src 1092, tests 19, data 1044, docs exact | narrow | medium |
| `tests/test_integration_completed_container_recovery.py` | integration-trains | legacy | libs 58 | src 1080, tests 14, data 1046, docs dir? | narrow | medium |
| `tests/test_integration_completion_recovery.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1036, docs exact | narrow | medium |
| `tests/test_integration_container_completion.py` | integration-trains | legacy | libs 58 | src 1080, tests 14, data 1039, docs dir? | narrow | medium |
| `tests/test_integration_containment_cutover.py` | integration-trains | legacy | libs 58 | src 1080, tests 8, data 1042, docs dir? | narrow | medium |
| `tests/test_integration_contracts.py` | integration-trains | legacy | libs 58 | src 1080, tests 5, data 1043, docs exact | narrow | medium |
| `tests/test_integration_cutover.py` | integration-trains | legacy | libs 58 | src 1080, tests 12, data 2083, docs dir? | narrow | medium |
| `tests/test_integration_delivery_watchdog.py` | integration-trains | legacy | libs 58 | src 1080, tests 5, data 1037, docs exact | narrow | medium |
| `tests/test_integration_delivery_watchdog_regressions.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1037, docs exact | narrow | medium |
| `tests/test_integration_dispatch_waits.py` | integration-trains | legacy | libs 58 | src 1092, tests 13, data 1044, docs dir? | narrow | medium |
| `tests/test_integration_failure_groups.py` | integration-trains | legacy | libs 58 | src 25, tests 4, data 182, docs none | narrow | medium |
| `tests/test_integration_fixers.py` | integration-trains | legacy | libs 58 | src 1080, tests 14, data 1050, docs dir? | narrow | medium |
| `tests/test_integration_git_truth.py` | integration-trains | legacy | libs 58 | src 189, tests 5, data 328, docs none | narrow | medium |
| `tests/test_integration_gitops.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1038, docs exact | narrow | medium |
| `tests/test_integration_hierarchy.py` | integration-trains | legacy | libs 58 | src 1088, tests 6, data 1052, docs exact | narrow | medium |
| `tests/test_integration_mode.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1041, docs exact | narrow | medium |
| `tests/test_integration_models.py` | integration-trains | legacy | libs 58 | src 192, tests 6, data 326, docs exact | narrow | medium |
| `tests/test_integration_observe.py` | integration-trains | legacy | libs 58 | src 1080, tests 5, data 1037, docs exact | narrow | medium |
| `tests/test_integration_operator_decisions.py` | integration-trains | legacy | libs 58 | src 1080, tests 10, data 1041, docs exact | narrow | medium |
| `tests/test_integration_outbox.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1040, docs exact | narrow | medium |
| `tests/test_integration_parent_subjects.py` | integration-trains | legacy | libs 58 | src 1081, tests 8, data 1036, docs exact | narrow | medium |
| `tests/test_integration_record_delivered.py` | integration-trains | legacy | libs 58 | src 1080, tests 12, data 1041, docs dir? | narrow | medium |
| `tests/test_integration_records_and_gates.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1037, docs exact | narrow | medium |
| `tests/test_integration_replay.py` | integration-trains | legacy | libs 58 | src 1080, tests 5, data 1038, docs exact | narrow | medium |
| `tests/test_integration_root_noop.py` | integration-trains | legacy | libs 58 | src 1080, tests 12, data 1041, docs dir? | narrow | medium |
| `tests/test_integration_sealing.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1036, docs exact | narrow | medium |
| `tests/test_integration_service.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1036, docs exact | narrow | medium |
| `tests/test_integration_size.py` | integration-trains | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 2035, docs dir? | narrow after declaration (unresolved dynamic import in escaped test helper module) | medium |
| `tests/test_integration_source_ancestry.py` | integration-trains | legacy | libs 58 | src 1080, tests 10, data 1041, docs exact | narrow | medium |
| `tests/test_integration_stacked_branches.py` | integration-trains | legacy | libs 58 | src 1080, tests 13, data 1042, docs dir? | narrow | medium |
| `tests/test_integration_stale_schedule.py` | integration-trains | legacy | libs 58 | src 1080, tests 7, data 1036, docs exact | narrow | medium |
| `tests/test_integration_stall_recovery.py` | integration-trains | legacy | libs 58 | src 1080, tests 8, data 1036, docs exact | narrow | medium |
| `tests/test_integration_state.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1036, docs exact | narrow | medium |
| `tests/test_integration_subjects.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1037, docs exact | narrow | medium |
| `tests/test_integration_superseded_completion.py` | integration-trains | legacy | libs 58 | src 1080, tests 6, data 1042, docs exact | narrow | medium |
| `tests/test_integration_train.py` | integration-trains | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 1053, docs dir? | narrow after declaration (unresolved dynamic import) | medium |
| `tests/test_integration_train_policy.py` | integration-trains | legacy | libs 58 | src 1080, tests 13, data 1038, docs dir? | narrow | medium |
| `tests/test_integration_train_sources.py` | integration-trains | legacy | libs 58 | src 1080, tests 18, data 1060, docs dir? | narrow | medium |
| `tests/test_integration_truth_reconciler.py` | integration-trains | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 1044, docs dir? | narrow after declaration (unresolved dynamic import in test helper lookup) | medium |
| `tests/test_integration_workspaces.py` | integration-trains | legacy | libs 58 | src 1081, tests 13, data 1039, docs exact | narrow | medium |

### B02 — integration promotion and ownership (55 modules)

Areas: integration-promotion, integration-ownership, integration-controls, epic-trains.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_agent_queue_train_policy.py` | epic-trains | legacy | libs 58 | src 1080, tests 5, data 869, docs exact | narrow | medium |
| `tests/test_canonical_reservation.py` | integration-ownership | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_cli_integration.py` | integration-controls | legacy | libs 58 | src 1080, tests 5, data 868, docs exact | narrow | medium |
| `tests/test_doctor_integration_checks.py` | integration-controls | conservative | 1224 | src 1081, tests 14, data 866, docs dir? | narrow | low |
| `tests/test_epic_branch.py` | epic-trains | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_epic_delivery.py` | epic-trains | conservative | 1224 | src 1080, tests 9, data 866, docs dir? | narrow | low |
| `tests/test_epic_dependencies.py` | epic-trains | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_epic_pr.py` | epic-trains | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_epic_pr_review_evidence.py` | epic-trains | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_epic_rebase_and_escalation.py` | epic-trains | conservative | 1224 | src 1080, tests 10, data 866, docs exact | narrow | low |
| `tests/test_epic_train_end_to_end.py` | epic-trains | conservative | 1224 | src 1080, tests 12, data 866, docs dir? | narrow | low |
| `tests/test_hierarchical_delivery_playbook.py` | epic-trains | conservative | 1224 | src 1080, tests 9, data 869, docs exact | narrow | low |
| `tests/test_integration_app_mode.py` | integration-controls | legacy | libs 58 | src 1080, tests 6, data 900, docs exact | narrow | medium |
| `tests/test_integration_cancelled_collection.py` | integration-promotion | legacy | libs 58 | src 1081, tests 6, data 902, docs exact | narrow | medium |
| `tests/test_integration_child_delivery.py` | integration-promotion | legacy | libs 58 | src 1081, tests 6, data 910, docs exact | narrow | medium |
| `tests/test_integration_delivered_parent_adoption.py` | integration-promotion | legacy | libs 58 | src 1080, tests 7, data 898, docs exact | narrow | medium |
| `tests/test_integration_eject.py` | integration-ownership | legacy | libs 58 | src 1080, tests 6, data 904, docs exact | narrow | medium |
| `tests/test_integration_epics.py` | integration-promotion | legacy | libs 58 | src 1080, tests 7, data 903, docs exact | narrow | medium |
| `tests/test_integration_finished_owners.py` | integration-ownership | legacy | libs 58 | src 1081, tests 6, data 902, docs exact | narrow | medium |
| `tests/test_integration_legacy_backfill.py` | integration-promotion | legacy | libs 58 | src 1081, tests 12, data 910, docs dir? | narrow | medium |
| `tests/test_integration_legacy_park_retirement.py` | integration-controls | legacy | libs 58 | src 1080, tests 7, data 908, docs dir? | narrow | medium |
| `tests/test_integration_lock.py` | integration-ownership | legacy | libs 58 | src 1080, tests 6, data 905, docs exact | narrow | medium |
| `tests/test_integration_manual_control.py` | integration-controls | legacy | libs 58 | src 1080, tests 12, data 892, docs dir? | narrow | medium |
| `tests/test_integration_owner_recovery.py` | integration-ownership | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 915, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_integration_owner_recovery_sweep.py` | integration-ownership | legacy | libs 58 | src 1080, tests 5, data 902, docs exact | narrow | medium |
| `tests/test_integration_owner_retry_recovery.py` | integration-ownership | legacy | libs 58 | src 190, tests 5, data 190, docs none | narrow | medium |
| `tests/test_integration_ownership.py` | integration-ownership | legacy | libs 58 | src 1080, tests 14, data 901, docs dir? | narrow | medium |
| `tests/test_integration_parent_completion.py` | integration-promotion | legacy | libs 58 | src 1080, tests 9, data 922, docs dir? | narrow | medium |
| `tests/test_integration_parent_source.py` | integration-promotion | legacy | libs 58 | src 1080, tests 9, data 900, docs exact | narrow | medium |
| `tests/test_integration_pr_delivery.py` | integration-promotion | legacy | libs 58 | src 1080, tests 6, data 908, docs exact | narrow | medium |
| `tests/test_integration_pr_repair_publication.py` | integration-promotion | legacy | libs 58 | src 1080, tests 6, data 904, docs exact | narrow | medium |
| `tests/test_integration_protection.py` | integration-controls | legacy | libs 58 | src 212, tests 5, data 186, docs exact | narrow | medium |
| `tests/test_integration_repair.py` | integration-promotion | legacy | libs 58 | src 1080, tests 13, data 931, docs exact | narrow | medium |
| `tests/test_integration_repair_lineage.py` | integration-promotion | legacy | libs 58 | src 190, tests 5, data 185, docs none | narrow | medium |
| `tests/test_integration_repair_rollover.py` | integration-promotion | legacy | libs 58 | src 1080, tests 9, data 901, docs exact | narrow | medium |
| `tests/test_integration_review_evidence.py` | integration-promotion | legacy | libs 58 | src 1080, tests 7, data 915, docs exact | narrow | medium |
| `tests/test_integration_root_authorization.py` | integration-promotion | legacy | libs 58 | src 1080, tests 8, data 897, docs exact | narrow | medium |
| `tests/test_integration_root_pull_requests.py` | integration-promotion | legacy | libs 58 | src 1080, tests 6, data 899, docs exact | narrow | medium |
| `tests/test_integration_stale_owners.py` | integration-ownership | legacy | libs 58 | src 1080, tests 6, data 900, docs exact | narrow | medium |
| `tests/test_integration_stranded_children.py` | integration-promotion | legacy | libs 58 | src 1081, tests 9, data 898, docs exact | narrow | medium |
| `tests/test_integration_train_promotion.py` | integration-promotion | legacy | libs 58 | src 1080, tests 17, data 907, docs dir? | narrow | medium |
| `tests/test_integration_transfer_commands.py` | integration-ownership | legacy | libs 58 | src 1080, tests 5, data 901, docs exact | narrow | medium |
| `tests/test_integration_trust_manifest.py` | integration-controls | legacy | libs 58 | src 1080, tests 6, data 900, docs exact | narrow | medium |
| `tests/test_integration_workspace_handoff.py` | integration-ownership | legacy | libs 58 | src 1080, tests 5, data 910, docs exact | narrow | medium |
| `tests/test_promote_commands.py` | integration-promotion | conservative | 1224 | src 1080, tests 13, data 867, docs dir? | narrow | low |
| `tests/test_promotion_backmerge.py` | integration-promotion | conservative | 1224 | src 1080, tests 14, data 866, docs dir? | narrow | low |
| `tests/test_promotion_continuous_equivalence.py` | integration-promotion | conservative | 1224 | src 1080, tests 6, data 870, docs exact | narrow | low |
| `tests/test_promotion_flow.py` | integration-promotion | conservative | 1224 | src 1081, tests 7, data 867, docs exact | narrow | low |
| `tests/test_promotion_notes_input.py` | integration-promotion | conservative | 1224 | src 1080, tests 14, data 866, docs dir? | narrow | low |
| `tests/test_promotion_policy.py` | integration-promotion | conservative | 1224 | src 1080, tests 16, data 867, docs dir? | narrow | low |
| `tests/test_promotion_request_playbook.py` | integration-promotion | conservative | 1224 | src 1080, tests 6, data 870, docs exact | narrow | low |
| `tests/test_promotion_rulesets.py` | integration-promotion | conservative | 1224 | src 1080, tests 5, data 868, docs exact | narrow | low |
| `tests/test_promotion_steps.py` | integration-promotion | conservative | 1224 | src 1080, tests 12, data 866, docs dir? | narrow | low |
| `tests/test_repair_checkout_refs.py` | integration-promotion | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_retained_holder_repair.py` | integration-ownership | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |

### B03 — git, GitHub and delivery (56 modules)

Areas: development-delivery, github, git-manager, git-identity, ci-sentinel, worktree-slots.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_api_pull_requests.py` | github | legacy | libs 58 | src 1082, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_branch_discard.py` | development-delivery | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_branch_retirement.py` | development-delivery | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_ci_baseline_status.py` | ci-sentinel | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_ci_completion.py` | ci-sentinel | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_ci_durations.py` | ci-sentinel | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_ci_main_sentinel.py` | ci-sentinel | conservative | 1224 | src 1080, tests 8, data 873, docs exact | narrow | low |
| `tests/test_ci_trigger_policy.py` | ci-sentinel | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 165, docs dir? | narrow after declaration (unresolved dynamic import in test helper lookup) | medium |
| `tests/test_close_unpushed_work.py` | development-delivery | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_concurrent_merge_and_push.py` | git-manager | conservative | 1224 | src 189, tests 6, data 156, docs none | narrow | medium |
| `tests/test_delivery_consumers.py` | development-delivery | legacy | libs 58 | src 1080, tests 7, data 2081, docs dir? | narrow | medium |
| `tests/test_delivery_integration.py` | development-delivery | legacy | libs 58 | src 1082, tests 7, data 900, docs exact | narrow | medium |
| `tests/test_delivery_latency.py` | development-delivery | legacy | libs 58 | src 1083, tests 5, data 896, docs exact | narrow | medium |
| `tests/test_delivery_precondition_refusals.py` | development-delivery | legacy | libs 58 | src 1080, tests 5, data 898, docs exact | narrow | medium |
| `tests/test_delivery_receipt_retirement.py` | development-delivery | legacy | libs 58 | src 211, tests 5, data 1373, docs none | narrow | medium |
| `tests/test_development_integration.py` | development-delivery | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_development_subjects.py` | development-delivery | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_force_with_lease.py` | git-manager | conservative | 1224 | src 189, tests 6, data 156, docs none | narrow | medium |
| `tests/test_git_app_auth.py` | github | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_git_command_handlers.py` | git-manager | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_git_events.py` | git-manager | conservative | 1224 | src 191, tests 6, data 155, docs none | narrow | medium |
| `tests/test_git_identity.py` | git-identity | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_git_manager.py` | git-manager | conservative | 1224 | src 189, tests 6, data 156, docs none | narrow | medium |
| `tests/test_git_manager_async.py` | git-manager | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_git_mutex.py` | git-manager | conservative | 1224 | src 189, tests 6, data 156, docs none | narrow | medium |
| `tests/test_git_seed_helpers.py` | git-manager | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 2395, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution; unresolved dynamic import in test helper lookup; unresolved file import) | medium |
| `tests/test_github_access_architecture.py` | github | conservative | 1224 | src 189, tests 5, data 160, docs none | narrow | medium |
| `tests/test_github_access_workflow.py` | github | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_github_app.py` | github | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_github_auth.py` | github | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_github_client.py` | github | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved dynamic import in test helper lookup) | medium |
| `tests/test_github_contracts.py` | github | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_github_issue_triage.py` | github | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_github_launcher_architecture.py` | github | conservative | 1224 | src 189, tests 5, data 1373, docs none | narrow | medium |
| `tests/test_github_repository_creation.py` | github | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_github_runner.py` | github | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_integration_properties.py` | development-delivery | legacy | libs 58 | src 1080, tests 16, data 1044, docs dir? | narrow | medium |
| `tests/test_merge_conflict_check.py` | git-manager | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_merge_slot.py` | git-manager | conservative | 1224 | src 1082, tests 5, data 867, docs exact | narrow | low |
| `tests/test_mid_chain_rebase.py` | git-manager | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pr_base_branch.py` | github | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_pr_merge_ci_gate.py` | github | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_pr_merge_command.py` | github | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_profile_github.py` | github | conservative | 1224 | src 229, tests 5, data 164, docs exact | narrow | medium |
| `tests/test_project_github.py` | github | conservative | 1224 | src 189, tests 5, data 157, docs none | narrow | medium |
| `tests/test_rebase_before_merge.py` | git-manager | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_set_default_branch_command.py` | git-manager | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_slot_report.py` | development-delivery | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_stranded_work.py` | development-delivery | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_sync_and_merge.py` | git-manager | conservative | 1224 | src 189, tests 6, data 156, docs none | narrow | medium |
| `tests/test_worktree_acquisition.py` | worktree-slots | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_worktree_doctor.py` | worktree-slots | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_worktree_manager.py` | worktree-slots | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_worktree_prepare.py` | worktree-slots | conservative | 1224 | src 1082, tests 5, data 868, docs exact | narrow | low |
| `tests/test_worktree_reaper.py` | worktree-slots | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_worktree_slot_starvation.py` | worktree-slots | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |

### B04 — migrations (61 modules)

Areas: migrations.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_doctor_db_checks.py` | migrations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_dv2_phase1_migration.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_migration.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_attestation_publications.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_boolean_defaults.py` | migrations | resolved | 17 | src 17, tests 4, data 147, docs none | narrow | medium |
| `tests/test_migration_candidate_authority.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_candidate_baseline.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_candidate_mutation_claims.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_capacity_spill.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_collaboration_threads.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_conflict_resolution.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_conversation_kind.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_digest_author_requests.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_epic_pull_requests.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_escalation_actions.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_escalation_message_direction.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_escalation_state.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_escalation_stateful_outcomes.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_guard.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_guard_restoration.py` | migrations | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_migration_handoff.py` | migrations | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_migration_human_asks.py` | migrations | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_migration_integration_cleanup.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_integration_cleanup_hardening.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_integration_controls.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_integration_promotion.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_integration_service.py` | migrations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_migration_integration_subjects.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_json_columns.py` | migrations | resolved | 25 | src 25, tests 4, data 146, docs none | narrow | medium |
| `tests/test_migration_metadata.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_parent_collection.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_parent_subjects.py` | migrations | conservative | 1224 | src 189, tests 7, data 155, docs none | narrow | medium |
| `tests/test_migration_playbook_blocked_lifecycle.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_playbook_building_blocks.py` | migrations | conservative | 1224 | src 1080, tests 11, data 866, docs exact | narrow | low |
| `tests/test_migration_playbook_v2.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_pool_claim_refusal.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_pool_claim_refusals.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_postgres_upgrade_head.py` | migrations | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_migration_project_router_binding.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_publication_delegations.py` | migrations | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_migration_repair_stages.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_retired_intelligence_classes.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_review_playbook_pin.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_review_withdrawal.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_root_main_promotion.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_schema_ahead.py` | migrations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_migration_session_hooks_provisioned.py` | migrations | resolved | 17 | src 17, tests 4, data 147, docs none | narrow | medium |
| `tests/test_migration_single_head.py` | migrations | resolved | 17 | src 17, tests 4, data 146, docs none | narrow | medium |
| `tests/test_migration_source_ci_infrastructure.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_string_defaults.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_subagent_events.py` | migrations | resolved | 25 | src 25, tests 4, data 10, docs none | narrow | medium |
| `tests/test_migration_supervisor_conversations.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_task_route_jsonb.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_task_route_source.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_test_selections.py` | migrations | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/test_migration_token_cache_columns.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migration_workspaces_v2.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_missing_fk_migration.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_task_completion_migration.py` | migrations | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_proposals_migration.py` | migrations | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_worktree_migration.py` | migrations | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |

### B05 — database, claims and pools (59 modules)

Areas: database, test-substrate, claims, scheduling, worker-pools, resource-gating, operator-workers, managed-jobs.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/perf/test_claim_statements.py` | claims | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/test_claim_commands.py` | claims | legacy | libs 58 | src 1081, tests 15, data 870, docs dir? | narrow | medium |
| `tests/test_cli_claim_restart.py` | claims | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_pool_provider.py` | worker-pools | legacy | libs 58 | src 1081, tests 4, data 866, docs exact | narrow | medium |
| `tests/test_database.py` | database | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_database_backend_selection.py` | database | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_database_channels.py` | database | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_database_engine.py` | database | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_database_imports.py` | database | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 155, docs dir? | broad: imports every database module in a subprocess | low (unchanged) |
| `tests/test_database_postgresql.py` | database | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_db_immediate.py` | database | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_db_pool_reaper.py` | test-substrate | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in escaped test helper module) | medium |
| `tests/test_import_cycles.py` | claims | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 155, docs dir? | broad: imports every src module in a subprocess | low (unchanged) |
| `tests/test_ingest_hold_admission.py` | claims | conservative | 1224 | src 1080, tests 6, data 2080, docs exact | narrow | low |
| `tests/test_jobs.py` | managed-jobs | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_jobs_attachments.py` | managed-jobs | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_jobs_matter.py` | managed-jobs | conservative | 1224 | src 1080, tests 12, data 866, docs exact | narrow | low |
| `tests/test_jobs_output.py` | managed-jobs | conservative | 1224 | src 195, tests 5, data 155, docs none | narrow | medium |
| `tests/test_jobs_queries.py` | managed-jobs | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_jobs_recovery.py` | managed-jobs | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_jobs_runner.py` | managed-jobs | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_jobs_verified_waits.py` | managed-jobs | conservative | 1224 | src 1092, tests 11, data 866, docs exact | narrow | low |
| `tests/test_jobs_waits.py` | managed-jobs | conservative | 1224 | src 1147, tests 12, data 869, docs dir? | narrow | low |
| `tests/test_metadata_cast_safety.py` | database | conservative | 1224 | src 1080, tests 5, data 2080, docs exact | narrow | low |
| `tests/test_migrate_sqlite_to_pg.py` | database | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_migrate_task_records.py` | database | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_models.py` | database | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_onboarding_queries.py` | database | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_operational_retention.py` | database | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_operator_workers.py` | operator-workers | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_pg_dsn.py` | test-substrate | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in escaped test helper module) | medium |
| `tests/test_pool_admin.py` | worker-pools | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pool_api_postgres.py` | worker-pools | conservative | 1224 | src 1146, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pool_doctor.py` | worker-pools | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_pool_lifecycle_integration.py` | worker-pools | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_pool_placement.py` | worker-pools | conservative | 1224 | src 194, tests 5, data 155, docs none | narrow | medium |
| `tests/test_pool_reconciler.py` | worker-pools | conservative | 1224 | src 1080, tests 10, data 866, docs dir? | narrow | low |
| `tests/test_pool_reconciler_carveouts.py` | worker-pools | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_pool_sizing.py` | worker-pools | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/test_postgres_test_substrate.py` | test-substrate | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved dynamic import in escaped test helper module) | medium |
| `tests/test_preparation_failure.py` | claims | conservative | 1224 | src 206, tests 5, data 155, docs none | narrow | medium |
| `tests/test_project_constraint_enforcement.py` | scheduling | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_constraints.py` | scheduling | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_schedules.py` | scheduling | conservative | 1224 | src 1086, tests 5, data 866, docs exact | narrow | low |
| `tests/test_reset_for_tests_guard.py` | test-substrate | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_resource_doctor.py` | resource-gating | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_resource_limits.py` | resource-gating | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_resource_test_runs.py` | resource-gating | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_schedule.py` | scheduling | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_scheduler.py` | scheduling | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_sqlite_removal.py` | database | conservative | 1224 | src 189, tests 5, data 1373, docs none | narrow | medium |
| `tests/test_stranded_feature_branches.py` | worker-pools | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_swarm_models.py` | worker-pools | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_swarm_surface.py` | worker-pools | conservative | 1224 | src 1080, tests 5, data 869, docs exact | narrow | low |
| `tests/test_task_session_pool_integration.py` | worker-pools | legacy | libs 58 | src 1080, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_test_baseline.py` | resource-gating | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_test_db_reaper.py` | test-substrate | conservative | 1224 | src 1081, tests 5, data 868, docs exact | narrow | low |
| `tests/test_timer_service.py` | scheduling | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_timer_service_cron.py` | scheduling | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |

### B06 — playbook engine and authoring (55 modules)

Areas: playbook-engine, playbook-definition, playbook-authoring.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_activation_source_scope.py` | playbook-authoring | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_agent_task_executor.py` | playbook-engine | legacy | libs 58 | src 1080, tests 7, data 867, docs exact | narrow | medium |
| `tests/test_api_playbook_v2_commands.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_child_task_reconciler.py` | playbook-engine | conservative | 1224 | src 1080, tests 10, data 867, docs exact | narrow | low |
| `tests/test_command_contracts_registry.py` | playbook-authoring | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 866, docs dir? | broad: loads every command contract in a subprocess | low (unchanged) |
| `tests/test_command_executor.py` | playbook-engine | conservative | 1224 | src 1080, tests 7, data 916, docs exact | narrow | low |
| `tests/test_list_playbooks_v2.py` | playbook-authoring | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_llm_executor.py` | playbook-engine | conservative | 1224 | src 1081, tests 7, data 867, docs exact | narrow | low |
| `tests/test_manual_playbook_runs.py` | playbook-engine | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_pkg5_scenarios.py` | playbook-engine | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_playbook_activation.py` | playbook-authoring | legacy | libs 58 | src 1082, tests 7, data 914, docs exact | narrow | medium |
| `tests/test_playbook_activation_commands.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 7, data 913, docs exact | narrow | medium |
| `tests/test_playbook_artifact_diff.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_playbook_artifact_ref.py` | playbook-definition | legacy | libs 58 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_playbook_artifact_store.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 915, docs exact | narrow | medium |
| `tests/test_playbook_collections.py` | playbook-definition | legacy | libs 58 | src 1080, tests 8, data 913, docs exact | narrow | medium |
| `tests/test_playbook_compiler_scope.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 7, data 866, docs exact | narrow | medium |
| `tests/test_playbook_delete.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 7, data 867, docs exact | narrow | medium |
| `tests/test_playbook_explanation.py` | playbook-definition | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_playbook_features.py` | playbook-definition | legacy | libs 58 | src 1081, tests 12, data 930, docs exact | narrow | medium |
| `tests/test_playbook_graph_projection.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 914, docs exact | narrow | medium |
| `tests/test_playbook_nested_loops.py` | playbook-engine | legacy | libs 58 | src 1081, tests 11, data 913, docs exact | narrow | medium |
| `tests/test_playbook_pending_events_commands.py` | playbook-engine | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_playbook_pending_events_policy.py` | playbook-engine | legacy | libs 58 | src 1081, tests 11, data 913, docs exact | narrow | medium |
| `tests/test_playbook_private_values.py` | playbook-engine | legacy | libs 58 | src 1080, tests 12, data 867, docs exact | narrow | medium |
| `tests/test_playbook_receipts.py` | playbook-engine | legacy | libs 58 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_playbook_run_authority.py` | playbook-engine | legacy | libs 58 | src 1080, tests 13, data 868, docs dir? | narrow | medium |
| `tests/test_playbook_run_overlay.py` | playbook-engine | legacy | libs 58 | src 1080, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_playbook_run_repository.py` | playbook-engine | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_playbook_script.py` | playbook-engine | legacy | libs 58 | src 1081, tests 12, data 913, docs exact | narrow | medium |
| `tests/test_playbook_script_recovery.py` | playbook-engine | legacy | libs 58 | src 1080, tests 11, data 867, docs exact | narrow | medium |
| `tests/test_playbook_script_runner.py` | playbook-engine | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_playbook_services.py` | playbook-engine | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_playbook_state.py` | playbook-engine | legacy | libs 58 | src 1080, tests 6, data 867, docs exact | narrow | medium |
| `tests/test_playbook_state_notify.py` | playbook-engine | legacy | libs 58 | src 1080, tests 12, data 868, docs dir? | narrow | medium |
| `tests/test_playbook_v2_api_dtos.py` | playbook-authoring | legacy | libs 58 | src 1086, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_playbook_v2_commands.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 6, data 914, docs exact | narrow | medium |
| `tests/test_playbook_v2_compiler.py` | playbook-authoring | legacy | libs 58 | src 1080, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_playbook_v2_definition.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 916, docs exact | narrow | medium |
| `tests/test_playbook_v2_expressions.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 913, docs exact | narrow | medium |
| `tests/test_playbook_v2_import.py` | playbook-authoring | legacy | libs 58 | src 1113, tests 7, data 913, docs exact | narrow | medium |
| `tests/test_playbook_v2_integration_policy.py` | playbook-definition | legacy | libs 58 | src 1080, tests 6, data 870, docs exact | narrow | medium |
| `tests/test_playbook_v2_resolution.py` | playbook-definition | legacy | libs 58 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_playbook_v2_validation.py` | playbook-definition | conservative | 1224 | src 1080, tests 6, data 913, docs exact | narrow | low |
| `tests/test_playbook_wait_repository.py` | playbook-engine | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_required_playbooks.py` | playbook-authoring | conservative | 1224 | src 1181, tests 5, data 867, docs exact | narrow | low |
| `tests/test_stuck_task_watch.py` | playbook-engine | conservative | 1224 | src 1081, tests 9, data 868, docs exact | narrow | low |
| `tests/test_stuck_task_watch_daemon.py` | playbook-engine | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 870, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_update_playbook_source_v2.py` | playbook-authoring | conservative | 1224 | src 1081, tests 6, data 867, docs exact | narrow | low |
| `tests/test_v2_dry_run.py` | playbook-engine | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_v2_engine.py` | playbook-engine | conservative | 1224 | src 1082, tests 7, data 877, docs exact | narrow | low |
| `tests/test_v2_engine_repository.py` | playbook-engine | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_v2_receipts.py` | playbook-engine | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_v2_restart_resume.py` | playbook-engine | conservative | 1224 | src 1081, tests 12, data 867, docs exact | narrow | low |
| `tests/test_v2_shadow.py` | playbook-engine | conservative | 1224 | src 1080, tests 7, data 867, docs exact | narrow | low |

### B07 — shipped playbooks, profiles and classes (58 modules)

Areas: shipped-playbooks, profiles, intelligence-classes, capabilities, policy-profiles, prompts.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_agent_profiles.py` | profiles | legacy | libs 58 | src 1113, tests 9, data 866, docs exact | narrow | medium |
| `tests/test_assignment_routing_building_blocks.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 9, data 870, docs exact | narrow | low |
| `tests/test_blocked_task_escalation.py` | shipped-playbooks | conservative | 1224 | src 1086, tests 5, data 872, docs exact | narrow | low |
| `tests/test_capability_operator_surfaces.py` | capabilities | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_capability_policy.py` | capabilities | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_class_retirement.py` | intelligence-classes | conservative | 1224 | src 192, tests 5, data 176, docs none | narrow | medium |
| `tests/test_cli_policy.py` | policy-profiles | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_codex_fast_classes.py` | intelligence-classes | conservative | 1224 | src 210, tests 5, data 156, docs none | narrow | medium |
| `tests/test_codex_tier_classes.py` | intelligence-classes | conservative | 1224 | src 210, tests 5, data 155, docs none | narrow | medium |
| `tests/test_command_capability_authorization.py` | capabilities | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_default_agent_type_playbooks.py` | shipped-playbooks | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_default_assignment_routing_playbook.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 7, data 868, docs exact | narrow | low |
| `tests/test_default_pipeline_equivalence.py` | shipped-playbooks | conservative | 1224 | src 1081, tests 6, data 873, docs exact | narrow | low |
| `tests/test_default_pipeline_intake.py` | shipped-playbooks | conservative | 1224 | src 1081, tests 11, data 868, docs exact | narrow | low |
| `tests/test_default_playbook_v2_artifacts.py` | shipped-playbooks | conservative | 1224 | src 1081, tests 6, data 897, docs dir? | narrow | low |
| `tests/test_default_profiles.py` | profiles | conservative | 1224 | src 221, tests 5, data 165, docs none | narrow | medium |
| `tests/test_delegation_no_widening.py` | capabilities | conservative | 1224 | src 1088, tests 6, data 866, docs exact | narrow | low |
| `tests/test_env_scrub.py` | capabilities | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_execution_principal.py` | capabilities | conservative | 1224 | src 1087, tests 5, data 866, docs exact | narrow | low |
| `tests/test_fast_smooth_train.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 7, data 872, docs exact | narrow | low |
| `tests/test_intelligence_class_deletion.py` | intelligence-classes | conservative | 1224 | src 1113, tests 5, data 866, docs exact | narrow | low |
| `tests/test_intelligence_class_editing.py` | intelligence-classes | conservative | 1224 | src 1113, tests 6, data 866, docs exact | narrow | low |
| `tests/test_intelligence_class_registry.py` | intelligence-classes | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_intelligence_classes.py` | intelligence-classes | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_playbook_v2_fixtures.py` | shipped-playbooks | legacy | libs 58 | src 191, tests 5, data 158, docs none | narrow | medium |
| `tests/test_policy_profiles.py` | policy-profiles | conservative | 1224 | src 1148, tests 7, data 870, docs exact | narrow | low |
| `tests/test_profile_catalog.py` | intelligence-classes | conservative | 1224 | src 206, tests 5, data 164, docs exact | narrow | medium |
| `tests/test_profile_default_class.py` | intelligence-classes | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_profile_drift.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_profile_functional.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_profile_inheritance.py` | profiles | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_profile_intelligence.py` | intelligence-classes | conservative | 1224 | src 206, tests 5, data 155, docs none | narrow | medium |
| `tests/test_profile_migration.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_profile_model_pin_migration.py` | profiles | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_profile_parser.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_profile_parser_runtime.py` | profiles | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_profile_session_fields.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_profile_sync.py` | profiles | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_profile_vault_write.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_defaults_playbook.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 7, data 879, docs dir? | narrow | low |
| `tests/test_project_override_migration.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_prompt_builder.py` | prompts | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_prompt_manager.py` | prompts | conservative | 1224 | src 190, tests 5, data 156, docs none | narrow | medium |
| `tests/test_prompt_task_templates.py` | prompts | conservative | 1224 | src 199, tests 5, data 155, docs none | narrow | medium |
| `tests/test_quilt_health_watch.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 6, data 883, docs exact | narrow | low |
| `tests/test_retired_defaults.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_session_class_effort.py` | intelligence-classes | legacy | libs 58 | src 209, tests 5, data 156, docs none | narrow | medium |
| `tests/test_session_intelligence_class.py` | intelligence-classes | legacy | libs 58 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_shipped_playbook_sources.py` | shipped-playbooks | conservative | 1224 | src 1081, tests 7, data 989, docs dir? | narrow | low |
| `tests/test_shipped_profile_capabilities.py` | capabilities | conservative | 1224 | src 1080, tests 5, data 881, docs exact | narrow | low |
| `tests/test_startup_profile_migration.py` | profiles | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_supervisor_capability_sync.py` | profiles | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_supervisor_failure_triage.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 6, data 873, docs exact | narrow | low |
| `tests/test_task_capability_inheritance.py` | capabilities | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_tier_budget_warnings.py` | prompts | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tier_provider_defaults.py` | intelligence-classes | conservative | 1224 | src 210, tests 5, data 156, docs none | narrow | medium |
| `tests/test_v1_removal.py` | shipped-playbooks | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_workspace_acquisition_read_only.py` | profiles | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |

### B08 — knowledge, memory and vault (60 modules)

Areas: knowledge-records, knowledge-evaluation, knowledge-inventory, memory-facts, vault.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_api_knowledge.py` | knowledge-records | legacy | libs 58 | src 1087, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_aq_uri.py` | vault | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_cli_vault.py` | vault | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_facts_bidirectional_sync.py` | memory-facts | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_facts_handler.py` | memory-facts | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_facts_parser.py` | memory-facts | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_file_watcher.py` | vault | conservative | 1224 | src 192, tests 5, data 156, docs exact | narrow | medium |
| `tests/test_knowledge_authority.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_authorization.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_budget.py` | knowledge-records | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_knowledge_capture.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_citations.py` | knowledge-records | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/test_knowledge_commands.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_compatibility.py` | knowledge-records | conservative | 1224 | src 1080, tests 9, data 866, docs exact | narrow | low |
| `tests/test_knowledge_context.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 867, docs exact | narrow | low |
| `tests/test_knowledge_deprecation.py` | knowledge-records | conservative | 1224 | src 1080, tests 9, data 866, docs exact | narrow | low |
| `tests/test_knowledge_evaluation.py` | knowledge-evaluation | resolved | 17 | src 17, tests 4, data 30, docs none | narrow | medium |
| `tests/test_knowledge_extraction.py` | knowledge-records | conservative | 1224 | src 1081, tests 8, data 866, docs exact | narrow | low |
| `tests/test_knowledge_extraction_store.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_harness_delivery.py` | knowledge-evaluation | conservative | 1224 | src 1081, tests 9, data 870, docs exact | narrow | low |
| `tests/test_knowledge_import.py` | knowledge-records | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/test_knowledge_import_inventory.py` | knowledge-records | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_index_receipts.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_inventory.py` | knowledge-inventory | resolved | 22 | src 22, tests 4, data 11, docs none | narrow | medium |
| `tests/test_knowledge_memory_adapter.py` | knowledge-records | conservative | 1224 | src 1082, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_migrations.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_proposals.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_provider_absence.py` | knowledge-records | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_redaction.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_retrieval.py` | knowledge-records | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_retrieval_benchmark.py` | knowledge-records | conservative | 1224 | src 1082, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_service.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_knowledge_task_creation.py` | knowledge-records | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_l0_l1_tier_injection.py` | memory-facts | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/test_mcp_memory_roundtrip.py` | memory-facts | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_memory_extension_points.py` | memory-facts | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_override_handler.py` | vault | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_override_injection.py` | memory-facts | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_readme_handler.py` | vault | conservative | 1224 | src 200, tests 5, data 157, docs none | narrow | medium |
| `tests/test_record_backfill.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_contracts.py` | knowledge-records | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_record_doctor.py` | knowledge-records | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_exports.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_graph.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_links.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_outbox.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_queries.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_record_schema.py` | knowledge-records | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_reference_stub_enricher.py` | vault | conservative | 1224 | src 210, tests 5, data 155, docs none | narrow | medium |
| `tests/test_reference_stub_regeneration.py` | vault | conservative | 1224 | src 211, tests 5, data 157, docs exact | narrow | medium |
| `tests/test_vault.py` | vault | conservative | 1224 | src 210, tests 5, data 158, docs none | narrow | medium |
| `tests/test_vault_glossary.py` | vault | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_vault_index.py` | vault | conservative | 1224 | src 209, tests 5, data 157, docs none | narrow | medium |
| `tests/test_vault_index_summaries.py` | vault | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_vault_manager.py` | vault | conservative | 1224 | src 211, tests 5, data 157, docs none | narrow | medium |
| `tests/test_vault_migration.py` | vault | conservative | 1224 | src 210, tests 5, data 158, docs none | narrow | medium |
| `tests/test_vault_watcher.py` | vault | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_vault_watcher_service.py` | vault | conservative | 1224 | src 195, tests 5, data 155, docs none | narrow | medium |
| `tests/test_wiki_links.py` | vault | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_workspace_spec_watcher.py` | vault | conservative | 1224 | src 191, tests 5, data 157, docs exact | narrow | medium |

### B09 — task lifecycle and routing (56 modules)

Areas: task-lifecycle, task-routing, task-hierarchy, task-dependencies, task-subtasks.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/perf/test_hierarchy_statements.py` | task-hierarchy | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_agent_task_routing.py` | task-routing | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_assignment_routing.py` | task-routing | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_cli_batch.py` | task-dependencies | legacy | libs 58 | src 1080, tests 4, data 866, docs exact | narrow | medium |
| `tests/test_cli_task_change_sets.py` | task-dependencies | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_task_create_output.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_task_subtasks.py` | task-subtasks | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cross_project_routing.py` | task-routing | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_deliverables.py` | task-lifecycle | conservative | 1224 | src 208, tests 5, data 157, docs exact | narrow | medium |
| `tests/test_dependency_queries.py` | task-dependencies | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_dv2_phase1_models.py` | task-lifecycle | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_ensure_task.py` | task-lifecycle | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_fidelity_review.py` | task-dependencies | conservative | 1224 | src 1081, tests 8, data 866, docs exact | narrow | low |
| `tests/test_get_downstream_tasks.py` | task-dependencies | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_get_stuck_tasks_command.py` | task-dependencies | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_commands.py` | task-hierarchy | conservative | 1224 | src 1086, tests 7, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_doctor.py` | task-hierarchy | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_ids.py` | task-hierarchy | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_queries.py` | task-hierarchy | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_settlement.py` | task-hierarchy | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_ingest_batch_queries.py` | task-dependencies | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_lifecycle_cleanup.py` | task-lifecycle | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_phases.py` | task-hierarchy | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pool_agent_routing.py` | task-routing | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_reopen_with_feedback.py` | task-lifecycle | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_routing_admission_v2.py` | task-routing | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_routing_doctor.py` | task-routing | conservative | 1224 | src 1081, tests 9, data 867, docs exact | narrow | low |
| `tests/test_routing_enforcement.py` | task-routing | conservative | 1224 | src 1080, tests 9, data 866, docs exact | narrow | low |
| `tests/test_routing_gate_guard.py` | task-routing | conservative | 1224 | src 234, tests 5, data 155, docs none | narrow | medium |
| `tests/test_routing_mandatory.py` | task-routing | conservative | 1224 | src 1113, tests 5, data 2097, docs dir? | narrow | low |
| `tests/test_routing_planner.py` | task-routing | conservative | 1224 | src 209, tests 5, data 158, docs exact | narrow | medium |
| `tests/test_routing_router.py` | task-routing | conservative | 1224 | src 1080, tests 8, data 867, docs exact | narrow | low |
| `tests/test_stall_watch.py` | task-lifecycle | conservative | 1224 | src 1080, tests 7, data 868, docs exact | narrow | low |
| `tests/test_state_machine.py` | task-lifecycle | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_task_batch_commands.py` | task-dependencies | legacy | libs 58 | src 1081, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_task_class_routing.py` | task-routing | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_close_accepted_tail.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 9, data 866, docs exact | narrow | medium |
| `tests/test_task_close_completion_recovery.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 7, data 866, docs exact | narrow | medium |
| `tests/test_task_close_summary_enforcement.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 6, data 867, docs exact | narrow | medium |
| `tests/test_task_command_authorization.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_context.py` | task-lifecycle | legacy | libs 58 | src 17, tests 4, data 10, docs none | narrow | medium |
| `tests/test_task_controls.py` | task-lifecycle | legacy | libs 58 | src 1113, tests 7, data 867, docs exact | narrow | medium |
| `tests/test_task_controls_api.py` | task-lifecycle | legacy | libs 58 | src 1115, tests 10, data 866, docs exact | narrow | medium |
| `tests/test_task_deletion.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 9, data 866, docs exact | narrow | medium |
| `tests/test_task_deps_command.py` | task-dependencies | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_doctor.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_lifecycle_properties.py` | task-lifecycle | legacy | libs 58 | src 1081, tests 10, data 866, docs exact | narrow | medium |
| `tests/test_task_progress_stats.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_ready_event.py` | task-lifecycle | legacy | libs 58 | src 1080, tests 7, data 866, docs exact | narrow | medium |
| `tests/test_task_route.py` | task-routing | legacy | libs 58 | src 1080, tests 9, data 866, docs exact | narrow | medium |
| `tests/test_task_route_source.py` | task-routing | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_routing_contract.py` | task-routing | legacy | libs 58 | src 1086, tests 8, data 867, docs exact | narrow | medium |
| `tests/test_task_split.py` | task-hierarchy | legacy | libs 58 | src 1080, tests 9, data 867, docs dir? | narrow | medium |
| `tests/test_task_subtask_commands.py` | task-subtasks | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_task_subtasks.py` | task-subtasks | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_worker_filing.py` | task-hierarchy | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |

### B10 — task graph, formulas and layout (52 modules)

Areas: task-graph-creation, work-graph, formulas, graph-layout, level-editor, object-loop, task-activity, task-archive, task-comments.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/perf/test_formula_statements.py` | formulas | conservative | 1224 | src 1080, tests 8, data 866, docs exact | narrow | low |
| `tests/perf/test_layout_api_statements.py` | graph-layout | conservative | 1224 | src 1085, tests 7, data 866, docs exact | narrow | low |
| `tests/perf/test_layout_statements.py` | graph-layout | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/task_graph/layout/test_active_expansion.py` | graph-layout | legacy | libs 58 | src 194, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_compaction.py` | graph-layout | legacy | libs 58 | src 1081, tests 7, data 892, docs exact | narrow | medium |
| `tests/task_graph/layout/test_cost.py` | graph-layout | legacy | libs 58 | src 193, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_engine_incremental.py` | graph-layout | legacy | libs 58 | src 198, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_engine_tidy.py` | graph-layout | legacy | libs 58 | src 198, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_flow.py` | graph-layout | legacy | libs 58 | src 193, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_layering.py` | graph-layout | legacy | libs 58 | src 192, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_order_key.py` | graph-layout | legacy | libs 58 | src 192, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_ordering.py` | graph-layout | legacy | libs 58 | src 193, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/layout/test_view.py` | graph-layout | legacy | libs 58 | src 194, tests 7, data 181, docs none | narrow | medium |
| `tests/task_graph/test_graph_commands.py` | graph-layout | legacy | libs 58 | src 1080, tests 6, data 892, docs exact | narrow | medium |
| `tests/task_graph/test_layout_dirty_marks.py` | graph-layout | legacy | libs 58 | src 1080, tests 6, data 895, docs exact | narrow | medium |
| `tests/task_graph/test_layout_driver.py` | graph-layout | legacy | libs 58 | src 1080, tests 6, data 894, docs exact | narrow | medium |
| `tests/task_graph/test_layout_queries.py` | graph-layout | legacy | libs 58 | src 1080, tests 6, data 892, docs exact | narrow | medium |
| `tests/task_graph/test_layout_step.py` | graph-layout | legacy | libs 58 | src 1080, tests 6, data 893, docs exact | narrow | medium |
| `tests/test_api_graph_layout.py` | graph-layout | legacy | libs 58 | src 1085, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_archive.py` | task-archive | conservative | 1224 | src 1080, tests 6, data 867, docs dir? | narrow | low |
| `tests/test_batch_checks.py` | task-graph-creation | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_batch_digest.py` | task-graph-creation | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_blocked_state.py` | work-graph | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_cli_task_comments.py` | task-comments | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_task_remove.py` | task-archive | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_create_task_graph_command.py` | task-graph-creation | conservative | 1224 | src 1080, tests 6, data 869, docs exact | narrow | low |
| `tests/test_editor_brush.py` | level-editor | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_editor_models.py` | level-editor | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_events_replay.py` | work-graph | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_explain.py` | work-graph | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_formula_commands.py` | formulas | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_formula_doctor.py` | formulas | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_formula_surface.py` | formulas | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_formulas_parse.py` | formulas | conservative | 1224 | src 211, tests 5, data 157, docs none | narrow | medium |
| `tests/test_formulas_provenance.py` | formulas | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_formulas_resolve.py` | formulas | conservative | 1224 | src 210, tests 5, data 157, docs none | narrow | medium |
| `tests/test_gate_queries.py` | work-graph | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_archive_delete.py` | task-archive | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_hierarchy_graph_creator.py` | task-graph-creation | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_object_loop.py` | object-loop | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_object_loop_playbook.py` | object-loop | conservative | 1224 | src 1082, tests 10, data 875, docs exact | narrow | low |
| `tests/test_task_activity.py` | task-activity | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_comments.py` | task-comments | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_task_comments_api.py` | task-comments | legacy | libs 58 | src 1115, tests 7, data 866, docs exact | narrow | medium |
| `tests/test_task_graph.py` | task-graph-creation | legacy | libs 58 | src 1080, tests 5, data 869, docs exact | narrow | medium |
| `tests/test_task_graph_activity.py` | task-graph-creation | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_task_graph_live_events.py` | task-graph-creation | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_task_removal.py` | task-archive | legacy | libs 58 | src 1080, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_task_session_attempts.py` | task-activity | legacy | libs 58 | src 1081, tests 7, data 866, docs exact | narrow | medium |
| `tests/test_work_graph_cascade.py` | work-graph | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_work_graph_commands.py` | work-graph | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_work_graph_outcomes.py` | work-graph | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |

### B11 — reviews, asks, escalations and digest (54 modules)

Areas: document-reviews, agent-questions, escalations, supervisor, digest.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_agent_flock_questions.py` | agent-questions | legacy | libs 58 | src 1113, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_agent_question_end_to_end.py` | agent-questions | legacy | libs 58 | src 1087, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_question_events.py` | agent-questions | legacy | libs 58 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_agent_question_native.py` | agent-questions | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_agent_question_websocket.py` | agent-questions | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_questions.py` | agent-questions | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_ask_api.py` | document-reviews | conservative | 1224 | src 1180, tests 16, data 866, docs exact | narrow | low |
| `tests/test_ask_commands.py` | document-reviews | conservative | 1224 | src 1081, tests 8, data 866, docs exact | narrow | low |
| `tests/test_ask_decisions.py` | document-reviews | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_ask_delivery.py` | document-reviews | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_ask_discussion.py` | document-reviews | conservative | 1224 | src 1081, tests 7, data 866, docs exact | narrow | low |
| `tests/test_ask_intake.py` | document-reviews | conservative | 1224 | src 1180, tests 15, data 866, docs exact | narrow | low |
| `tests/test_ask_legacy_reset.py` | document-reviews | conservative | 1224 | src 1081, tests 10, data 866, docs exact | narrow | low |
| `tests/test_ask_migration.py` | document-reviews | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_ask_operations.py` | document-reviews | conservative | 1224 | src 1113, tests 7, data 866, docs exact | narrow | low |
| `tests/test_ask_sources.py` | document-reviews | conservative | 1224 | src 1081, tests 14, data 867, docs exact | narrow | low |
| `tests/test_cli_questions.py` | agent-questions | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_digest.py` | digest | conservative | 1224 | src 218, tests 5, data 155, docs none | narrow | medium |
| `tests/test_digest_commands.py` | digest | conservative | 1224 | src 1114, tests 5, data 866, docs exact | narrow | low |
| `tests/test_digest_dispatch.py` | digest | conservative | 1224 | src 1084, tests 6, data 866, docs exact | narrow | low |
| `tests/test_digest_queries.py` | digest | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_digest_supervisor.py` | digest | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_discord_settings.py` | digest | conservative | 1224 | src 219, tests 5, data 155, docs none | narrow | medium |
| `tests/test_escalation_commands.py` | escalations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_escalation_delivery.py` | escalations | conservative | 1224 | src 1083, tests 6, data 866, docs exact | narrow | low |
| `tests/test_escalation_intake.py` | escalations | conservative | 1224 | src 1081, tests 5, data 867, docs exact | narrow | low |
| `tests/test_escalation_queries.py` | escalations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_escalation_state.py` | escalations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_escalation_sweep.py` | escalations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_morning_reports.py` | digest | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 871, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_proposal_api.py` | document-reviews | conservative | 1224 | src 1088, tests 6, data 866, docs exact | narrow | low |
| `tests/test_report_delivery.py` | digest | conservative | 1224 | src 1081, tests 7, data 868, docs exact | narrow | low |
| `tests/test_report_requests.py` | digest | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_review_commands.py` | document-reviews | conservative | 1224 | src 1116, tests 5, data 866, docs exact | narrow | low |
| `tests/test_review_diff.py` | document-reviews | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_review_doctor.py` | document-reviews | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_review_feedback.py` | document-reviews | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_review_gating.py` | document-reviews | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_review_keys.py` | document-reviews | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_review_notifier.py` | document-reviews | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_review_playbooks.py` | document-reviews | conservative | 1224 | src 1082, tests 6, data 868, docs exact | narrow | low |
| `tests/test_review_profile_rules.py` | document-reviews | conservative | 1224 | src 189, tests 5, data 170, docs none | narrow | medium |
| `tests/test_review_queries.py` | document-reviews | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_review_reopen_cascade.py` | document-reviews | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_review_service.py` | document-reviews | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_review_vault.py` | document-reviews | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_spec_approve_command.py` | document-reviews | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_supervisor_global_lifecycle.py` | supervisor | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_supervisor_inbox_commands.py` | supervisor | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_supervisor_inbox_say.py` | supervisor | conservative | 1224 | src 1180, tests 9, data 866, docs exact | narrow | low |
| `tests/test_supervisor_recovery.py` | supervisor | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_supervisor_recovery_api.py` | supervisor | conservative | 1224 | src 1115, tests 9, data 866, docs exact | narrow | low |
| `tests/test_supervisor_stall_sweep_script.py` | supervisor | conservative | 1224 | src 189, tests 5, data 157, docs none | narrow | medium |
| `tests/test_supervisor_turn_health.py` | supervisor | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |

### B12 — messages, Discord and events (54 modules)

Areas: messages, messaging-adapters, discord, discord-conversations, collaboration, events.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_api_messages.py` | messages | legacy | libs 58 | src 1146, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_cli_messages.py` | messages | legacy | libs 58 | src 1080, tests 5, data 867, docs exact | narrow | medium |
| `tests/test_collaboration.py` | collaboration | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_collaboration_commands.py` | collaboration | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_collaboration_lifecycle.py` | collaboration | conservative | 1224 | src 1086, tests 7, data 866, docs exact | narrow | low |
| `tests/test_collaboration_message_wait.py` | collaboration | conservative | 1224 | src 1114, tests 9, data 867, docs dir? | narrow | low |
| `tests/test_collaboration_messages.py` | collaboration | conservative | 1224 | src 1114, tests 8, data 867, docs dir? | narrow | low |
| `tests/test_collaboration_queries.py` | collaboration | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_collaboration_waits.py` | collaboration | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_conversation_backfill.py` | discord-conversations | conservative | 1224 | src 1091, tests 6, data 866, docs exact | narrow | low |
| `tests/test_conversation_intake.py` | discord-conversations | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_conversation_limits.py` | discord-conversations | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_conversation_maintenance.py` | discord-conversations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_conversation_outbox.py` | discord-conversations | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_conversation_preconditions.py` | discord-conversations | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_conversation_queries.py` | discord-conversations | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_conversation_render.py` | discord-conversations | conservative | 1224 | src 210, tests 5, data 155, docs none | narrow | medium |
| `tests/test_conversation_wiring.py` | discord-conversations | conservative | 1224 | src 1180, tests 8, data 866, docs exact | narrow | low |
| `tests/test_dashboard_links.py` | messaging-adapters | conservative | 1224 | src 218, tests 5, data 155, docs none | narrow | medium |
| `tests/test_discord_adapter.py` | discord | conservative | 1224 | src 1093, tests 5, data 866, docs exact | narrow | low |
| `tests/test_discord_ask_buttons.py` | discord | conservative | 1224 | src 1082, tests 8, data 866, docs exact | narrow | low |
| `tests/test_discord_bot_routing.py` | discord | conservative | 1224 | src 1091, tests 5, data 866, docs exact | narrow | low |
| `tests/test_discord_cutover.py` | discord | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_discord_docs.py` | discord | conservative | 1224 | src 1081, tests 5, data 2129, docs dir? | narrow | low |
| `tests/test_discord_escalation_buttons.py` | discord | conservative | 1224 | src 1091, tests 6, data 866, docs exact | narrow | low |
| `tests/test_discord_escalation_transport.py` | discord | conservative | 1224 | src 208, tests 5, data 155, docs none | narrow | medium |
| `tests/test_discord_housekeeping.py` | discord | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_discord_inbound_router.py` | discord | conservative | 1224 | src 1091, tests 7, data 866, docs exact | narrow | low |
| `tests/test_discord_intake_diagnostics.py` | discord | conservative | 1224 | src 1091, tests 5, data 866, docs exact | narrow | low |
| `tests/test_discord_render_budgets.py` | discord | conservative | 1224 | src 220, tests 5, data 155, docs none | narrow | medium |
| `tests/test_discord_simplification_lifecycle.py` | discord | conservative | 1224 | src 1097, tests 5, data 866, docs exact | narrow | low |
| `tests/test_discord_task_delete.py` | discord | conservative | 1224 | src 1081, tests 9, data 866, docs exact | narrow | low |
| `tests/test_emit_schema_compliance.py` | events | conservative | 1224 | src 197, tests 5, data 172, docs none | narrow | medium |
| `tests/test_event_bus.py` | events | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_event_bus_validation.py` | events | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_event_commands.py` | events | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_event_field_contracts.py` | events | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_event_schema_registry_validation.py` | events | conservative | 1224 | src 197, tests 5, data 1373, docs dir? | narrow | medium |
| `tests/test_event_schemas.py` | events | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_message_commands.py` | messages | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_message_delivery.py` | messages | conservative | 1224 | src 1080, tests 6, data 881, docs dir? | narrow | low |
| `tests/test_message_queries.py` | messages | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_message_sender_identity.py` | messages | conservative | 1224 | src 1090, tests 7, data 867, docs exact | narrow | low |
| `tests/test_messaging_adapter.py` | messaging-adapters | conservative | 1224 | src 1093, tests 5, data 866, docs exact | narrow | low |
| `tests/test_messaging_port.py` | messaging-adapters | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_notifications.py` | messaging-adapters | conservative | 1224 | src 200, tests 5, data 155, docs none | narrow | medium |
| `tests/test_null_messaging_boot.py` | messaging-adapters | conservative | 1224 | src 1096, tests 5, data 866, docs exact | narrow | low |
| `tests/test_proposal_status_event.py` | events | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_rate_guard.py` | discord | conservative | 1224 | src 1091, tests 5, data 866, docs exact | narrow | low |
| `tests/test_remote_links.py` | messaging-adapters | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_session_lens.py` | messages | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_slash_commands.py` | discord | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_validate_event.py` | events | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_websocket_forwarding.py` | events | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |

### B13 — sessions, tmux and transcripts (56 modules)

Areas: session-runtime, tmux-terminals, panes-streams, harnesses, session-checkpoints, transcripts, prime.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_checkpoint_c2.py` | session-checkpoints | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_checkpoint_c3.py` | session-checkpoints | conservative | 1224 | src 1083, tests 5, data 866, docs exact | narrow | low |
| `tests/test_cli_agent_surface.py` | prime | legacy | libs 58 | src 1088, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_session_history.py` | session-runtime | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_streams.py` | panes-streams | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_vault_reset_harness.py` | harnesses | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_codex_trust.py` | session-runtime | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_handoffs.py` | prime | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_harness_manifest.py` | harnesses | conservative | 1224 | src 191, tests 5, data 156, docs none | narrow | medium |
| `tests/test_harness_parser.py` | harnesses | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |
| `tests/test_message_pane_open_column.py` | panes-streams | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_message_send_pane_open.py` | panes-streams | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pane_broadcaster.py` | panes-streams | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_pane_stream_api.py` | panes-streams | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_prime_hook_envelopes.py` | prime | conservative | 1224 | src 201, tests 5, data 155, docs none | narrow | medium |
| `tests/test_prime_renderer.py` | prime | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_prime_session_workspace.py` | prime | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_resume_key_validation.py` | session-runtime | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_runtimes_base.py` | session-runtime | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_runtimes_registry.py` | session-runtime | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_session_close_notifications.py` | session-runtime | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_session_commands.py` | session-runtime | conservative | 1224 | src 1080, tests 12, data 867, docs dir? | narrow | low |
| `tests/test_session_doctor.py` | session-runtime | legacy | libs 58 | src 1080, tests 9, data 866, docs exact | narrow | medium |
| `tests/test_session_executable.py` | session-runtime | legacy | libs 58 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_session_input.py` | session-runtime | legacy | libs 58 | src 1113, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_session_launch.py` | session-runtime | legacy | libs 58 | src 1080, tests 5, data 2080, docs exact | narrow | medium |
| `tests/test_session_opencode_liveness.py` | session-runtime | legacy | libs 58 | src 212, tests 5, data 155, docs none | narrow | medium |
| `tests/test_session_provider_conformance.py` | session-runtime | legacy | libs 58 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_session_queries.py` | session-runtime | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_session_reconciler.py` | session-runtime | legacy | libs 58 | src 1081, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_session_runtime_units.py` | session-runtime | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_session_spec.py` | session-runtime | legacy | libs 58 | src 1080, tests 7, data 867, docs exact | narrow | medium |
| `tests/test_session_stream_api.py` | panes-streams | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_session_tool_allowlist.py` | session-runtime | legacy | libs 58 | src 209, tests 5, data 156, docs none | narrow | medium |
| `tests/test_streams_api.py` | panes-streams | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_streams_registry.py` | panes-streams | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_terminal_host_shell.py` | tmux-terminals | conservative | 1224 | src 194, tests 5, data 155, docs none | narrow | medium |
| `tests/test_terminal_input.py` | tmux-terminals | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_terminal_pty.py` | tmux-terminals | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_terminal_stream.py` | tmux-terminals | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_tmux_harness_wake.py` | tmux-terminals | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tmux_integration.py` | tmux-terminals | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tmux_nudge_confirm.py` | tmux-terminals | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tmux_nudge_drafts.py` | tmux-terminals | conservative | 1224 | src 194, tests 6, data 155, docs none | narrow | medium |
| `tests/test_tmux_nudge_recovery.py` | tmux-terminals | conservative | 1224 | src 222, tests 7, data 155, docs none | narrow | medium |
| `tests/test_tmux_opencode_composer.py` | tmux-terminals | conservative | 1224 | src 194, tests 6, data 155, docs none | narrow | medium |
| `tests/test_tmux_path.py` | tmux-terminals | conservative | 1224 | src 209, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tmux_startup_dialogs.py` | tmux-terminals | conservative | 1224 | src 194, tests 6, data 155, docs none | narrow | medium |
| `tests/test_transcript_checkpoints.py` | transcripts | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_transcript_codex.py` | transcripts | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |
| `tests/test_transcript_completions.py` | transcripts | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |
| `tests/test_transcript_identity.py` | transcripts | conservative | 1224 | src 193, tests 5, data 155, docs none | narrow | medium |
| `tests/test_transcript_readers.py` | transcripts | conservative | 1224 | src 200, tests 5, data 157, docs none | narrow | medium |
| `tests/test_transcript_retention.py` | transcripts | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_transcript_watcher.py` | transcripts | conservative | 1224 | src 1081, tests 5, data 867, docs exact | narrow | low |
| `tests/test_usage_limit_screen.py` | tmux-terminals | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |

### B14 — agent waits, roster and workspaces (55 modules)

Areas: agent-waits, agent-roster, agent-cron, workspaces.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_agent_cron.py` | agent-cron | legacy | libs 58 | src 1113, tests 8, data 867, docs dir? | narrow | medium |
| `tests/test_agent_deletion_supply.py` | agent-roster | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_agent_flock.py` | agent-roster | legacy | libs 58 | src 1113, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_flock_lifecycle.py` | agent-roster | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_liveness.py` | agent-roster | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_names.py` | agent-roster | legacy | libs 58 | src 1081, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_reconciler.py` | agent-roster | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_agent_soft_delete.py` | agent-roster | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_agent_subagents.py` | agent-roster | legacy | libs 58 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_agent_terminals.py` | agent-roster | legacy | libs 58 | src 1113, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_agent_wait_commands.py` | agent-waits | legacy | libs 58 | src 1081, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_agent_wait_queries.py` | agent-waits | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_agent_waits.py` | agent-waits | legacy | libs 58 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_base_workspace_guard.py` | workspaces | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_branch_isolated_workspace.py` | workspaces | conservative | 1224 | src 1082, tests 6, data 866, docs exact | narrow | low |
| `tests/test_cli_task_create_requires_kinds.py` | workspaces | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_create_task_requires_kinds.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_resolve_repo_path.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_smart_workspace_assignment.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_subagent_events.py` | agent-roster | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_task_requirements_queries.py` | workspaces | legacy | libs 58 | src 1080, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_task_session_queries.py` | agent-roster | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_wait_admission.py` | agent-waits | legacy | libs 58 | src 1092, tests 7, data 965, docs exact | narrow | medium |
| `tests/test_wait_commands.py` | agent-waits | legacy | libs 58 | src 1100, tests 13, data 968, docs exact | narrow | medium |
| `tests/test_wait_cutover.py` | agent-waits | legacy | libs 58 | src 1092, tests 14, data 956, docs exact | narrow | medium |
| `tests/test_wait_decisions.py` | agent-waits | legacy | libs 58 | src 1092, tests 16, data 958, docs exact | narrow | medium |
| `tests/test_wait_dispatch.py` | agent-waits | conservative | 1224 | src 1092, tests 12, data 961, docs exact | narrow | low |
| `tests/test_wait_faults.py` | agent-waits | legacy | libs 58 | src 1101, tests 28, data 963, docs exact | narrow | medium |
| `tests/test_wait_graph.py` | agent-waits | legacy | libs 58 | src 1080, tests 5, data 954, docs exact | narrow | medium |
| `tests/test_wait_hibernation.py` | agent-waits | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 971, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_wait_holding.py` | agent-waits | legacy | libs 58 | src 1080, tests 7, data 954, docs exact | narrow | medium |
| `tests/test_wait_legacy_bridge.py` | agent-waits | legacy | libs 58 | src 1092, tests 8, data 954, docs exact | narrow | medium |
| `tests/test_wait_listener.py` | agent-waits | legacy | libs 58 | src 210, tests 5, data 243, docs none | narrow | medium |
| `tests/test_wait_metrics.py` | agent-waits | legacy | libs 58 | src 1082, tests 5, data 956, docs exact | narrow | medium |
| `tests/test_wait_migration.py` | agent-waits | legacy | libs 58 | src 187, tests 5, data 101, docs none | narrow | medium |
| `tests/test_wait_outcomes.py` | agent-waits | legacy | libs 58 | src 1092, tests 14, data 954, docs exact | narrow | medium |
| `tests/test_wait_queries.py` | agent-waits | legacy | libs 58 | src 1080, tests 6, data 953, docs exact | narrow | medium |
| `tests/test_wait_resource_sources.py` | agent-waits | legacy | libs 58 | src 1080, tests 6, data 955, docs exact | narrow | medium |
| `tests/test_wait_source_queries.py` | agent-waits | legacy | libs 58 | src 1080, tests 6, data 953, docs exact | narrow | medium |
| `tests/test_wait_surfaces.py` | agent-waits | legacy | libs 58 | src 1125, tests 15, data 955, docs exact | narrow | medium |
| `tests/test_waits.py` | agent-waits | legacy | libs 58 | src 1080, tests 6, data 960, docs exact | narrow | medium |
| `tests/test_workspace.py` | workspaces | conservative | 1224 | src 192, tests 5, data 156, docs none | narrow | medium |
| `tests/test_workspace_attachments.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_workspace_branch_wait.py` | workspaces | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_workspace_command_edges.py` | workspaces | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_workspace_commands.py` | workspaces | conservative | 1224 | src 1082, tests 6, data 866, docs exact | narrow | low |
| `tests/test_workspace_control_file_exclude.py` | workspaces | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_workspace_kind_parser.py` | workspaces | conservative | 1224 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_workspace_kind_registry.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_workspace_kinds_queries.py` | workspaces | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_workspace_listing.py` | workspaces | conservative | 1224 | src 193, tests 5, data 156, docs none | narrow | medium |
| `tests/test_workspace_names.py` | workspaces | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_workspace_sentinel_reclaim.py` | workspaces | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_workspace_slot_affinity.py` | workspaces | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_workspace_sync_comprehensive.py` | workspaces | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |

### B15 — providers, LLM client, metrics and orchestrator (54 modules)

Areas: provider-failover, provider-usage, llm-client, metrics, logging, orchestrator.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/llm/test_cli.py` | llm-client | conservative | 1224 | src 205, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_client.py` | llm-client | conservative | 1224 | src 207, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_config.py` | llm-client | conservative | 1224 | src 189, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_fallback.py` | llm-client | conservative | 1224 | src 1081, tests 9, data 867, docs exact | narrow | low |
| `tests/llm/test_gemini_adapter.py` | llm-client | conservative | 1224 | src 204, tests 7, data 155, docs none | narrow | medium |
| `tests/llm/test_openai_adapter.py` | llm-client | conservative | 1224 | src 204, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_provider_requests.py` | llm-client | conservative | 1224 | src 204, tests 7, data 155, docs none | narrow | medium |
| `tests/llm/test_providers.py` | llm-client | conservative | 1224 | src 205, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_run_tools.py` | llm-client | conservative | 1224 | src 207, tests 6, data 155, docs none | narrow | medium |
| `tests/llm/test_spec.py` | llm-client | conservative | 1224 | src 204, tests 6, data 155, docs none | narrow | medium |
| `tests/test_api_metrics.py` | metrics | legacy | libs 58 | src 1084, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_api_perf_middleware.py` | metrics | legacy | libs 58 | src 1146, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_api_provider_usage.py` | provider-usage | legacy | libs 58 | src 1147, tests 6, data 867, docs exact | narrow | medium |
| `tests/test_budget.py` | provider-usage | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_chat_analyzer_metrics.py` | metrics | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_claude_usage.py` | provider-usage | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_claude_usage_parser.py` | provider-usage | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_constraint_enforcement.py` | orchestrator | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_costs.py` | provider-usage | conservative | 1224 | src 1113, tests 6, data 866, docs exact | narrow | low |
| `tests/test_dashboard_perf_kit.py` | metrics | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 868, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_engine_perf_observer.py` | metrics | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_fake_session_script.py` | provider-failover | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_llm_logger.py` | logging | conservative | 1224 | src 204, tests 5, data 155, docs none | narrow | medium |
| `tests/test_llm_usage.py` | provider-usage | conservative | 1224 | src 207, tests 5, data 155, docs none | narrow | medium |
| `tests/test_log_access.py` | logging | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_logging_config.py` | logging | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in test helper lookup) | medium |
| `tests/test_metrics_histogram.py` | metrics | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_metrics_host.py` | metrics | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_metrics_perf.py` | metrics | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_metrics_relay.py` | metrics | conservative | 1224 | src 187, tests 5, data 13, docs none | narrow | medium |
| `tests/test_metrics_sampler.py` | metrics | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_metrics_token_efficiency.py` | metrics | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_orchestrator.py` | orchestrator | conservative | 1224 | src 1083, tests 23, data 914, docs dir? | narrow | low |
| `tests/test_orchestrator_llm.py` | llm-client | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_plugin_invoke_llm.py` | llm-client | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_provider_allocation.py` | provider-failover | conservative | 1224 | src 1082, tests 8, data 866, docs exact | narrow | low |
| `tests/test_provider_availability.py` | provider-failover | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_provider_availability_api.py` | provider-failover | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_provider_availability_doctor.py` | provider-failover | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_provider_commands.py` | provider-failover | conservative | 1224 | src 1086, tests 6, data 866, docs exact | narrow | low |
| `tests/test_provider_doctor.py` | provider-usage | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_provider_escalation.py` | provider-failover | conservative | 1224 | src 1080, tests 7, data 866, docs exact | narrow | low |
| `tests/test_provider_evidence.py` | provider-failover | conservative | 1224 | src 1081, tests 6, data 866, docs exact | narrow | low |
| `tests/test_provider_failover_playbook.py` | provider-failover | conservative | 1224 | src 1080, tests 6, data 871, docs exact | narrow | low |
| `tests/test_provider_inflight.py` | provider-failover | conservative | 1224 | src 1080, tests 9, data 866, docs exact | narrow | low |
| `tests/test_provider_intent.py` | provider-failover | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 867, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_provider_reroute.py` | provider-failover | conservative | 1224 | src 1082, tests 8, data 866, docs exact | narrow | low |
| `tests/test_provider_spill.py` | provider-failover | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_provider_suppression.py` | provider-failover | conservative | 1224 | src 1080, tests 9, data 866, docs exact | narrow | low |
| `tests/test_provider_usage_activity_doctor.py` | provider-usage | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_provider_usage_probe.py` | provider-usage | conservative | 1224 | src 1080, tests 7, data 870, docs exact | narrow | low |
| `tests/test_provider_usage_queries.py` | provider-usage | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_task_failed_event.py` | orchestrator | legacy | libs 58 | src 1080, tests 8, data 866, docs exact | narrow | medium |
| `tests/test_workflow_pipeline_view.py` | orchestrator | conservative | 1224 | src 192, tests 6, data 155, docs none | narrow | medium |

### B16 — CLI, command dispatch, API and MCP tools (55 modules)

Areas: cli-framework, command-dispatch, daemon-cli, cli-test-runner, api-contract, api-scope, tools-mcp.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_api_auth.py` | api-scope | legacy | libs 58 | src 1116, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_api_client_contract.py` | api-contract | legacy | libs 58 | src 1147, tests 5, data 1421, docs exact | narrow | medium |
| `tests/test_api_codegen_input_models.py` | api-contract | legacy | libs 58 | src 1086, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_api_dashboard_pointer.py` | api-contract | legacy | libs 58 | src 1180, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_api_execute_contract.py` | api-contract | legacy | libs 58 | src 1088, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_api_scope.py` | api-scope | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_bazel_cli.py` | cli-test-runner | conservative | 1224 | src 1080, tests 4, data 868, docs exact | narrow | low |
| `tests/test_cli.py` | cli-framework | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_cli_client_generated.py` | api-contract | legacy | libs 58 | src 192, tests 5, data 155, docs none | narrow | medium |
| `tests/test_cli_conformance.py` | cli-framework | legacy | libs 58 | src 1081, tests 4, data 866, docs exact | narrow | medium |
| `tests/test_cli_daemon.py` | daemon-cli | legacy | libs 58 | src 1080, tests 4, data 866, docs exact | narrow | medium |
| `tests/test_cli_envelope.py` | cli-framework | legacy | libs 58 | src 1087, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_formatters.py` | cli-framework | legacy | libs 58 | src 220, tests 5, data 155, docs none | narrow | medium |
| `tests/test_cli_global_options.py` | cli-framework | legacy | libs 58 | src 1080, tests 5, data 867, docs exact | narrow | medium |
| `tests/test_cli_inventory.py` | cli-framework | legacy | libs 58 | src 1081, tests 4, data 867, docs exact | narrow | medium |
| `tests/test_cli_knowledge.py` | cli-framework | legacy | libs 58 | src 1080, tests 5, data 868, docs exact | narrow | medium |
| `tests/test_cli_logs.py` | cli-framework | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_menus.py` | cli-framework | legacy | libs 58 | src 20, tests 4, data 10, docs none | narrow | medium |
| `tests/test_cli_module_entry.py` | cli-framework | legacy | libs 58 | src 1080, tests 5, data 917, docs exact | narrow | medium |
| `tests/test_cli_module_map.py` | cli-framework | legacy | libs 58 | src 189, tests 5, data 208, docs none | narrow | medium |
| `tests/test_cli_response_failures.py` | cli-framework | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_cli_startup_offline.py` | cli-framework | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 867, docs dir? | broad: imports the whole CLI app (startup inventory) | low (unchanged) |
| `tests/test_cli_stop_sessions.py` | daemon-cli | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_structured_params.py` | cli-framework | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_test_runner.py` | cli-test-runner | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 875, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_command_descriptors.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 925, docs exact | narrow | low |
| `tests/test_command_dispatch.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_command_invoked_event.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_command_scope_isolation.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_command_scope_matrix.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_command_surface.py` | command-dispatch | conservative | 1224 | src 1087, tests 5, data 2080, docs exact | narrow | low |
| `tests/test_embedded_mcp.py` | tools-mcp | conservative | 1224 | src 1147, tests 5, data 866, docs exact | narrow | low |
| `tests/test_generated_command_contracts.py` | command-dispatch | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 868, docs dir? | broad: regenerates contracts from every command | low (unchanged) |
| `tests/test_main_lifecycle.py` | daemon-cli | conservative | 1224 | src 1180, tests 5, data 866, docs exact | narrow | low |
| `tests/test_mcp_catalog.py` | tools-mcp | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_mcp_commands.py` | tools-mcp | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_mcp_inline_migration.py` | tools-mcp | conservative | 1224 | src 191, tests 5, data 156, docs none | narrow | medium |
| `tests/test_mcp_probe.py` | tools-mcp | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_mcp_registry.py` | tools-mcp | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_mcp_server.py` | tools-mcp | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_regenerate_api_client_script.py` | api-contract | conservative | 1224 | src 189, tests 5, data 161, docs none | narrow | medium |
| `tests/test_regenerate_ts_client_script.py` | api-contract | conservative | 1224 | src 189, tests 5, data 160, docs none | narrow | medium |
| `tests/test_resource_semaphore.py` | cli-test-runner | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 155, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_response_model_registry.py` | api-contract | conservative | 1224 | src 1113, tests 5, data 866, docs exact | narrow | low |
| `tests/test_reviewer_api_scope.py` | api-scope | conservative | 1224 | src 1116, tests 11, data 866, docs exact | narrow | low |
| `tests/test_supervisor_global_scope.py` | api-scope | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_supervisor_global_token_loopback.py` | api-scope | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_surface_commands.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_system_commands.py` | command-dispatch | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_system_commands_prompt_path.py` | command-dispatch | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_target_scope.py` | api-scope | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_tool_index.py` | tools-mcp | conservative | 1224 | src 191, tests 5, data 155, docs none | narrow | medium |
| `tests/test_tool_registry.py` | tools-mcp | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_triage_api_scope.py` | api-scope | conservative | 1224 | src 1115, tests 6, data 866, docs exact | narrow | low |
| `tests/test_worker_git_scope.py` | api-scope | conservative | 1224 | src 1080, tests 8, data 867, docs dir? | narrow | low |

### B17 — config, install, doctor and projects (62 modules)

Areas: config, install, packaging, doctor, projects.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_additive_migrations.py` | install | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_case_insensitive_paths.py` | packaging | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_cli_projects.py` | projects | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_cli_system_config.py` | config | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_compat.py` | packaging | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 156, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution) | medium |
| `tests/test_config.py` | config | conservative | 1224 | src 189, tests 5, data 155, docs exact | narrow | medium |
| `tests/test_config_editor.py` | config | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_config_profiles.py` | config | conservative | 1224 | src 1180, tests 5, data 866, docs exact | narrow | low |
| `tests/test_config_roundtrip.py` | config | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_config_schema_inventory.py` | config | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 156, docs dir? | broad: inventories every config surface | low (unchanged) |
| `tests/test_config_secrets.py` | config | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_config_section_roundtrip.py` | config | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_config_supervisor.py` | config | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_config_tuning.py` | config | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_config_validation.py` | config | conservative | 1224 | src 1180, tests 5, data 866, docs exact | narrow | low |
| `tests/test_config_watcher.py` | config | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_db_cli.py` | install | conservative | 1224 | src 1080, tests 4, data 866, docs exact | narrow | low |
| `tests/test_doctor.py` | doctor | conservative | 1224 | src 1082, tests 9, data 866, docs exact | narrow | low |
| `tests/test_doctor_cli_timeout.py` | doctor | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_doctor_discord_config.py` | doctor | conservative | 1224 | src 1081, tests 4, data 866, docs exact | narrow | low |
| `tests/test_doctor_observations.py` | doctor | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_doctor_perf_checks.py` | doctor | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_doctor_skill_checks.py` | doctor | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_doctor_stall_checks.py` | doctor | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_health_platform.py` | doctor | conservative | 1224 | src 1180, tests 5, data 866, docs exact | narrow | low |
| `tests/test_install_cli.py` | install | conservative | 1224 | src 1082, tests 6, data 868, docs exact | narrow | low |
| `tests/test_install_dashboard.py` | install | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_install_engine.py` | install | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved dynamic import in test helper lookup) | medium |
| `tests/test_install_git_identity.py` | install | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_install_graft.py` | install | conservative | 1224 | src 51, tests 4, data 14, docs exact | narrow | medium |
| `tests/test_install_integration.py` | install | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_install_lifecycle.py` | install | conservative | 1224 | src 207, tests 4, data 16, docs exact | narrow | medium |
| `tests/test_install_logins.py` | install | conservative | 1224 | src 205, tests 5, data 157, docs exact | narrow | medium |
| `tests/test_install_macos.py` | install | conservative | 1224 | src 1080, tests 4, data 867, docs dir? | narrow | low |
| `tests/test_install_node_toolchain.py` | install | resolved | 19 | src 19, tests 4, data 10, docs none | narrow | medium |
| `tests/test_install_onboarding.py` | install | conservative | 1224 | src 1080, tests 4, data 867, docs dir? | narrow | low |
| `tests/test_install_platform.py` | install | resolved | 19 | src 19, tests 4, data 11, docs exact | narrow | medium |
| `tests/test_install_pools.py` | install | conservative | 1224 | src 204, tests 5, data 157, docs exact | narrow | medium |
| `tests/test_install_postgres.py` | install | conservative | 1224 | src 213, tests 4, data 16, docs dir? | narrow | medium |
| `tests/test_install_providers.py` | install | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_install_service.py` | install | conservative | 1224 | src 1081, tests 4, data 866, docs exact | narrow | low |
| `tests/test_install_wizard.py` | install | conservative | 1224 | src 43, tests 4, data 12, docs exact | narrow | medium |
| `tests/test_install_wsl.py` | install | conservative | 1224 | src 200, tests 5, data 156, docs exact | narrow | medium |
| `tests/test_known_tools.py` | install | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_observation_snapshots.py` | doctor | conservative | 1224 | src 1082, tests 5, data 866, docs exact | narrow | low |
| `tests/test_packaging_entry_points.py` | packaging | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_portable_config.py` | config | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_onboarding_commands.py` | projects | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_onboarding_contract.py` | projects | conservative | 1224 | src 1147, tests 5, data 868, docs exact | narrow | low |
| `tests/test_project_onboarding_service.py` | projects | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_project_relocation.py` | projects | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 867, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_project_repository_binding.py` | projects | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_roots_config.py` | config | conservative | 1224 | src 1081, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_supervisors.py` | projects | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_release_artifact.py` | packaging | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 866, docs dir? | broad: builds the release artifact from the tree | low (unchanged) |
| `tests/test_repository_checkout_repair.py` | projects | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_setup_script.py` | install | conservative | 1224 | src 189, tests 5, data 156, docs none | narrow | medium |
| `tests/test_terminal_origin_config.py` | config | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_uninstall_cli.py` | install | conservative | 1224 | src 1080, tests 4, data 866, docs exact | narrow | low |
| `tests/test_update.py` | install | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 868, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_update_rollback.py` | install | conservative | 1224 | src 1080, tests 5, data 868, docs exact | narrow | low |
| `tests/test_windows_bootstrap.py` | install | conservative | 1224 | src 189, tests 5, data 158, docs none | narrow | medium |

### B18 — dashboard, plugins, selection, Bazel, docs guards and e2e (58 modules)

Areas: dashboard-server, dashboard-state, plugins, file-access, test-selection, bazel-environment, bazel-metrics, bazel-runtime, bazel-toolchains, docs-guards, generated-artifacts, e2e-kit.

| module | area | current | today src | proposed | class | risk |
|---|---|---|---|---|---|---|
| `tests/test_api_graph.py` | dashboard-state | legacy | libs 58 | src 1082, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_api_task_files.py` | file-access | legacy | libs 58 | src 1082, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_api_workspace_files.py` | file-access | legacy | libs 58 | src 1082, tests 6, data 866, docs exact | narrow | medium |
| `tests/test_bazel_environment.py` | bazel-environment | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 159, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_bazel_manifest.py` | bazel-toolchains | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 183, docs dir? | broad: validates the generated manifest against every source | low (unchanged) |
| `tests/test_bazel_metrics.py` | bazel-metrics | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 11, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_bazel_runtime.py` | bazel-runtime | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 11, docs dir? | broad: executes the staged Bazel runtime in a subprocess | low (unchanged) |
| `tests/test_bazel_subprocess_dependencies.py` | bazel-toolchains | conservative | 1224 | src 189, tests 5, data 159, docs dir? | narrow | medium |
| `tests/test_bazel_test_dependencies.py` | bazel-toolchains | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 184, docs dir? | broad: runs the dependency analyzer over the repository | low (unchanged) |
| `tests/test_cli_dashboard_server.py` | dashboard-server | legacy | libs 58 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved dynamic import in subprocess Python execution; unresolved file import) | medium |
| `tests/test_cli_plugins.py` | plugins | legacy | libs 58 | src 1080, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_command_docs.py` | docs-guards | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 1448, docs dir? | broad: inventories every command against its docs | low (unchanged) |
| `tests/test_dashboard_browser_storage.py` | dashboard-state | conservative | 1224 | src 189, tests 5, data 786, docs none | narrow | medium |
| `tests/test_dashboard_secure_context.py` | dashboard-state | conservative | 1224 | src 189, tests 5, data 784, docs none | narrow | medium |
| `tests/test_dashboard_server_app.py` | dashboard-server | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_dashboard_server_bundle.py` | dashboard-server | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 156, docs dir? | broad: builds the dashboard bundle in a subprocess | low (unchanged) |
| `tests/test_dashboard_server_edge.py` | dashboard-server | conservative | 1224 | src 190, tests 4, data 13, docs none | narrow | medium |
| `tests/test_dashboard_server_proxy.py` | dashboard-server | conservative | 1224 | src 192, tests 6, data 155, docs none | narrow | medium |
| `tests/test_dashboard_state.py` | dashboard-state | conservative | 1224 | src 1146, tests 6, data 866, docs exact | narrow | low |
| `tests/test_docs_sync.py` | docs-guards | conservative | 1224 | src 189, tests 5, data 156, docs exact | narrow | medium |
| `tests/test_doctor_dashboard_server.py` | dashboard-server | conservative | 1224 | declare the loaded file/module, then src 1224, tests 1046, data 866, docs dir? | narrow after declaration (unresolved file import) | medium |
| `tests/test_documentation_checks.py` | docs-guards | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 156, docs dir? | broad: checks every doc link and reference | low (unchanged) |
| `tests/test_documentation_coverage.py` | docs-guards | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 156, docs dir? | broad: scans every doc against every command and config surface | low (unchanged) |
| `tests/test_e2e_cli_stateful.py` | e2e-kit | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 159, docs dir? | broad: drives the real CLI and daemon end to end | low (unchanged) |
| `tests/test_e2e_kit_fixtures.py` | e2e-kit | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 874, docs dir? | broad: e2e kit fixtures exercise the whole stateful CLI | low (unchanged) |
| `tests/test_e2e_probe.py` | e2e-kit | conservative | 1224 | all src (conservative, recorded reason); docs/**, tests 1046, data 161, docs dir? | broad: e2e probe loads the CLI surface by path | low (unchanged) |
| `tests/test_factory_policy_instructions.py` | docs-guards | conservative | 1224 | src 192, tests 5, data 320, docs dir? | narrow | medium |
| `tests/test_file_command_handlers.py` | file-access | conservative | 1224 | src 1080, tests 6, data 867, docs exact | narrow | low |
| `tests/test_generated_artifacts.py` | generated-artifacts | conservative | 1224 | src 189, tests 5, data 165, docs none | narrow | medium |
| `tests/test_guidance_docs.py` | docs-guards | conservative | 1224 | src 1080, tests 5, data 897, docs dir? | narrow | low |
| `tests/test_inbox_plugin.py` | plugins | conservative | 1224 | src 216, tests 5, data 155, docs none | narrow | medium |
| `tests/test_integration_troubleshooting_doc.py` | docs-guards | legacy | libs 58 | src 189, tests 5, data 208, docs exact | narrow | medium |
| `tests/test_notes_plugin.py` | plugins | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_pane_registry_parity.py` | dashboard-state | conservative | 1224 | src 191, tests 5, data 168, docs none | narrow | medium |
| `tests/test_plugin_commands.py` | plugins | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_plugin_cron_to_playbook_migration.py` | plugins | conservative | 1224 | src 218, tests 5, data 155, docs exact | narrow | medium |
| `tests/test_plugin_external_service_access.py` | plugins | conservative | 1224 | src 195, tests 5, data 155, docs none | narrow | medium |
| `tests/test_plugin_git_diff_stat.py` | plugins | conservative | 1224 | src 243, tests 5, data 156, docs none | narrow | medium |
| `tests/test_plugin_service_registration.py` | plugins | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_plugin_services.py` | plugins | conservative | 1224 | src 1080, tests 6, data 866, docs exact | narrow | low |
| `tests/test_plugins.py` | plugins | conservative | 1224 | src 1080, tests 5, data 866, docs exact | narrow | low |
| `tests/test_project_paths.py` | file-access | conservative | 1224 | src 190, tests 5, data 155, docs none | narrow | medium |
| `tests/test_script_modes.py` | e2e-kit | conservative | 1224 | src 189, tests 5, data 161, docs none | narrow | medium |
| `tests/test_selection_catalogue.py` | test-selection | conservative | 1224 | src 1080, tests 4, data 871, docs exact | narrow | low |
| `tests/test_selection_commands.py` | test-selection | conservative | 1224 | src 1146, tests 7, data 869, docs exact | narrow | low |
| `tests/test_selection_mandatory.py` | test-selection | conservative | 1224 | src 51, tests 5, data 15, docs exact | narrow | medium |
| `tests/test_selection_policy.py` | test-selection | conservative | 1224 | src 58, tests 5, data 12, docs exact | narrow | medium |
| `tests/test_selection_queries.py` | test-selection | conservative | 1224 | src 1080, tests 5, data 869, docs exact | narrow | low |
| `tests/test_selection_questions.py` | test-selection | conservative | 1224 | src 56, tests 5, data 12, docs exact | narrow | medium |
| `tests/test_selection_replay.py` | test-selection | conservative | 1224 | src 200, tests 6, data 158, docs exact | narrow | medium |
| `tests/test_selection_retention_sweep.py` | test-selection | conservative | 1224 | src 1080, tests 5, data 867, docs exact | narrow | low |
| `tests/test_selection_service.py` | test-selection | conservative | 1224 | src 1080, tests 6, data 868, docs exact | narrow | low |
| `tests/test_selection_snapshot.py` | test-selection | conservative | 1224 | src 188, tests 5, data 15, docs exact | narrow | medium |
| `tests/test_selection_static_impact.py` | test-selection | conservative | 1224 | src 190, tests 5, data 16, docs exact | narrow | medium |
| `tests/test_selection_testmon_experiment.py` | test-selection | conservative | 1224 | src 189, tests 5, data 155, docs none | narrow | medium |
| `tests/test_selection_typesafe.py` | test-selection | conservative | 1224 | src 197, tests 5, data 155, docs none | narrow | medium |
| `tests/test_task_attachments_api.py` | file-access | legacy | libs 58 | src 1146, tests 5, data 866, docs exact | narrow | medium |
| `tests/test_vibecop_plugin.py` | plugins | conservative | 1224 | src 216, tests 5, data 155, docs none | narrow | medium |
