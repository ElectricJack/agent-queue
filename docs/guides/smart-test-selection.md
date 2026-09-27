# Smart test selection

`aq test --aq-smart` proposes, and — once enforcement is on — runs, the
smallest safe test selection for your change. This guide is the operator's
reference: what the three modes do, how to switch on each, and what is
recorded for the later promotion decision.

The design spec is the authority: [smart-test-selection design](../superpowers/specs/2026-09-25-smart-test-selection-design.md).
Every key and its default lives in [the `test_selection` configuration
section](../reference/configuration.md#smart-test-selection-test_selection).

## What shadow records, and what it does not verify

`aq test --aq-smart` (mode `shadow`) computes the selection, records it, and
then runs exactly the modules it proposed. Two properties are worth being
precise about:

- **It records a proposal, not a verdict.** A shadow run with no explicit
  targets exits 2 after printing the report — it verifies nothing, by design.
  It exists so the daemon accumulates evidence (which areas Jev judged safe,
  which module a red build actually lived in) before anyone is allowed to
  omit tests on its say-so.
- **It does not substitute for anything.** The focused and area checks your
  task names, the marker arms (`perf`, `integration`, …) and the acceptance
  commands are unchanged. Shadow is an observation feed; it earns nothing by
  itself.

`--aq-smart=enforce` goes further: the run is limited to the selected modules
(the daemon refuses `-k`, `-m`, `--lf` and node-id narrowing alongside it), and
a selection that fell back to the whole suite is a refusal, not a run.

## The three switches

All live under the `test_selection` section, hot-reloadable (re-read on every
`test_select` call):

| switch | default | effect |
|---|---|---|
| `enabled` | `false` | master switch; off, `--aq-smart` is a usage error (exit 2) |
| `jev_enabled` | `false` | permit Jev's per-area omission judgments; off, only the static closure runs |
| `enforce_enabled` | `false` | permit `--aq-smart=enforce`; off, enforce is a usage error |

The key itself is never stored: `api_key_env` names the environment variable
(default `TYPESAFE_API_KEY`) that must hold it, and an unresolvable name is a
configuration error, not a fallback.

## Flags and exit codes

| flag | meaning |
|---|---|
| `--aq-smart` / `--aq-smart=shadow` | compute, record and run the proposed selection |
| `--aq-smart=enforce` | same, but the run is limited to the proposal and refusal conditions apply |
| `--aq-plan-only` | print the report; start nothing (exit 0) |
| `--aq-base <ref>` | comparison base (default: `origin/<default branch>` via `origin/HEAD`) |
| `--aq-no-jev` | static fallback only; records Jev as disabled |
| `--aq-project <id>` | operator run outside a worker session |

All the usual `--aq-*` slot flags (`--aq-wait`, `--aq-timeout`, `--aq-workers`
…) still apply on the non-smart path. Exit codes:

| code | meaning |
|---|---|
| 0 | ran (or `--aq-plan-only`: reported, nothing started) |
| 2 | usage refusal — `--aq-smart` off or missing required config, enforce narrowing, or a shadow run with no targets |
| 3 | daemon unreachable in enforce mode (a shadow run degrades to the explicit targets instead) |
| 4 | `selection_stale`, `full_suite_required` or `empty_selection` |
| 75 | the test slot never freed, as for every `aq test` run |

A path-shaped argument that does not exist is refused before pytest starts
(exit 2); a run that collected nothing exits nonzero and says so — both as
with the ordinary runner.

## The selection formula, and the reason codes

Each candidate module lands in exactly one bucket:

- **M — mandatory.** Changed test files, direct imports, scoped `conftest.py`,
  rules-named critical modules: it runs, full stop.
- **S — static impact.** The import closure of your changed sources.
- **J — Jev's judgment.** Per-area, from the model's affected / confidently-
  unaffected verdicts.
- **F — fallback.** Everything else that a red build could live in, when the
  inputs were incomplete or Jev abstained.

The run is `M ∪ S ∪ (J affected)`; when any input is `full_required` (a global
invalidator, an unmapped path, an unusable catalogue) the whole suite runs
instead, and in enforce mode that is a refusal (exit 4).

Every decision is stamped with a reason code (paths and areas named, never
content), in `src/test_selection/reasons.py`:

| group | codes |
|---|---|
| mandatory | `mandatory_changed_test`, `mandatory_direct_import`, `mandatory_rule`, `mandatory_scoped_conftest`, `mandatory_source_scan`, `mandatory_critical` |
| whole universe | `global_invalidator`, `snapshot_incomplete`, `unmapped_path`, `catalogue_unusable` |
| static | `static_impact`, `static_unavailable` |
| Jev | `jev_affected`, `jev_unknown`, `jev_confident_unaffected`, `jev_disabled`, `jev_unconfigured`, `jev_missing_answer`, `jev_invalid`, `jev_model_drift`, `jev_cached` |
| fallback | `fallback_timeout`, `fallback_budget`, `fallback_connection`, `fallback_http_401`, `fallback_http_422`, `fallback_http_429`, `fallback_http_529`, `fallback_http_other`, `packing_overflow` |
| caller | `narrowing_flags`, `explicit_target` |

The report (`--aq-plan-only` or the tail of a normal smart run) shows the
bucket counts (M/S/J/F/final), `full_required`, `jev_status`, the top reasons,
the proposed commands and any pending obligations.

## Inspecting records

```bash
aq test-selection list --project-id <project_id>
aq test-selection show --selection-id <selection_id>
aq test-selection policy-show --project-id <project_id>
```

A recorded selection carries: its `selection_id` (cite it on the task), the
base and head SHAs, the dirty/snapshot fingerprints, the catalogue / rules /
policy digests, the question schema version, the full bucket breakdown, every
reason code, the proposed `argv`, elapsed time, usage and a `pending_obligations`
list (the checks the selection itself could not run).

## Catalogue and rules

The mandatory/rule inputs are authored, not learned:

- `tests/selection_areas.yaml` — the area map every test module belongs to.
- `tests/selection_rules.yaml` — the named rules: mandatory globs, critical
  modules, global invalidators.
- `tests/selection_catalogue.json` — the generated digest of the map.

After adding or moving a test module:

```bash
python scripts/generate-selection-catalogue.py
```

and commit the catalogue. `tests/test_selection_catalogue.py` fails the build
when a tracked file is unmapped — an unmapped path is also the runtime's own
`unmapped_path` reason, not a silent omission.

## Promotion, and why it is still off

A **promotion** is the operator's statement that, for one project, Jev may
omission-judge for real (it is what would ever stand behind
`enforce_enabled: true`). It is identity-pinned:

```bash
aq test-selection promote --project-id <project_id> --model jev-1.13.0 \
  --question-schema-version <n> --catalogue-digest <d> --rules-digest <d> \
  --policy-digest <d> --evidence '<json>'
```

A promotion is valid only for the digests and model it was minted for —
change any of them and it is stale, not a gap to be patched.

**Rollback is a revoke.** Revoke names the promotion and the reason, and is
immediate:

```bash
aq test-selection revoke --promotion-id <promotion_id> --reason "<why>"
```

Nothing here promotes anything automatically. The bar (spec §7, *proposed*,
not yet met): the replay report over recorded red builds must show at least 30
usable held-out cases with at least 98 % of change-caused failing modules
selected by `M ∪ S ∪ J` — and zero critical misses. Until a replay report says
that, shadow stays an observation feed and enforcement stays off.

## What enforcement never replaces

Even fully promoted, the selection is a substitute for *breadth*, not for
*authority*. It never replaces:

- the CI arms — the selection is computed in a worktree from
  `origin/HEAD`; CI checks, replayed red builds and the full-suite baseline
  note remain the record of truth for "is main green";
- the marker arms — `perf`, `integration`, `tmux`-gated and the
  other slow-by-nature markers stay under the same opt-in they have always
  been under;
- the acceptance commands your task names — `npm --prefix dashboard test`,
  the e2e swarm smoke, or whatever else the task names — they run, with their
  own exit codes, exactly as written.

When a selection is stale, full-suite or empty, that is the run telling you
its scope is the whole suite: run your task's focused and area checks and
report the requirement, the same as today.
