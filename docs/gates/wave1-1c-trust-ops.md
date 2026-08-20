---
tags: [gate]
---

# Wave 1 Lane 1C — Trust & Ops

First use of the evidence-file convention from
`docs/specs/design/trust-and-ops.md` §8. Convention only — no tooling gates on
this file.

## Acceptance criteria

Defined up front from `docs/specs/implementation/trust-and-ops.md` §7, minus the
items that depend on unlanded workstreams:

1. `src/env_scrub.py` exists as a pure function with constants and `ScrubResult`;
   `isolated_env` becomes a thin wrapper over it.
2. `_cmd_run_command` / `_run_subprocess_shell` accept and pass a scrubbed env —
   the one real trust-boundary violation in the tree is *contained* per R1/R6.
3. `_validate_ref` guards every ref-accepting `GitManager` API (R4); the `--`
   audit is applied where git supports it.
4. `src/doctor/` ships models, a concurrent runner with per-check timeouts, and
   the generic built-in checks; reserved ids for unlanded subsystems report INFO.
5. `OpsCommandsMixin` fills in Wave 0's empty `src/commands/ops_commands.py` with
   `_cmd_doctor` and `_cmd_get_costs`; both get typed tool definitions.
6. `aq doctor` / `aq costs` exist with exit codes 0/1/2/3.
7. `record_token_usage` takes the model + split; `get_cost_rollup` groups by
   project / profile / day; unpriced rows are reported, never estimated.
8. Invariant tests land, including a docs-sync test that forces
   `docs/specs/database.md` up to date.
9. **Zero new test failures** against the pre-change baseline. A green suite was
   not an acceptance criterion — the suite does not pass on this machine.

Out of scope by direction: the golden harness scaffold and anything needing
session-runtime.

## Test evidence

Baseline (this worktree at `1ae1a65a`, before any lane-1C change):

```
python -m pytest tests/ -n auto -q -p no:randomly --tb=no
117 failed, 6730 passed, 469 skipped, 23 xfailed
```

After lane 1C, run twice (the suite is not deterministic under `-n auto`):

```
run 1: 113 failed, 6968 passed, 469 skipped, 23 xfailed
run 2: 111 failed, 6970 passed, 469 skipped, 23 xfailed
```

**Attributable new failures: 0.** Set difference against the baseline:

| Test | run 1 | run 2 | Verdict |
|---|---|---|---|
| `test_playbook_runner.py::TestPauseTimeoutSpec::test_re_pause_resets_paused_at` | fail | fail | flaky |
| `test_playbook_runner.py::TestPauseTimeoutSpec::test_resume_and_re_pause_at_same_node` | fail | fail | flaky |
| `test_l0_l1_tier_injection.py::TestL0RoleFromProfile` ×2 | fail | pass | flaky |
| `test_config_editor.py` ×8 | pass | pass | flaky (fails at baseline) |

Both clusters were reproduced as pre-existing flakes on untouched code:

- The two `TestPauseTimeoutSpec` failures assert `paused_at_second >
  paused_at_first` across two rapid `time.time()` calls. On Windows the clock
  granularity is ~15.6 ms, so both reads return the same float — the observed
  assertion is literally `1787186124.0606163 > 1787186124.0606163`. Run in
  isolation they fail 2 times in 3. `src/playbooks/runner.py` is not in this
  lane's diff.
- `test_l0_l1_tier_injection.py` passes 3/3 sequentially and fails **11 of 23**
  under `pytest -n 4` on the file alone, with a different subset each time —
  shared global state, not a behaviour change. The same mechanism explains the
  8 `test_config_editor.py` failures that appear at baseline and disappear
  afterwards; neither file's dependencies are in this lane's diff.

Targeted runs:

Targeted runs:

| Suite | Result |
|---|---|
| `tests/test_env_scrub.py` | 46 passed |
| `tests/test_doctor.py` | 68 passed |
| `tests/test_costs.py` | 39 passed |
| `tests/test_docs_sync.py` | 6 passed (failed 4/6 before the doc update, as the spec predicted) |
| `tests/test_command_surface.py` | 14 passed |
| `tests/test_git_manager_async.py` | 82 passed (was 55) |
| `tests/test_event_schema_registry_validation.py` | 234 passed |
| `tests/test_state_machine.py` | 294 passed |
| `pytest -k git` | 4 failed — all four are in the baseline failure list |

Lint: `ruff check` clean over every touched file. (`ruff format` is not enforced
in this repo — 61 files under `src/` are unformatted at `main`.)

Manual checks:

- `aq doctor` and `aq costs` register as top-level Click commands; the
  auto-generated variants remain reachable as `aq system doctor` /
  `aq system get-costs`, matching how `system config` already behaves.
- `python -c "import src.main"` and the CLI import path both load with the
  `DoctorRegistry` wiring in place.

## Spec diff

Specs were written first (`99f80798`); this lane only records outcomes and
resolves ambiguities:

- `docs/specs/implementation/trust-and-ops.md` — §5.5 gains the concrete
  contributed-check registration contract (reserved-id table, the three clauses
  an owner must satisfy, the test that pins them); §7 checklist marked, with the
  golden harness scaffold explicitly deferred and the Wave 0 migration noted as
  already landed.
- `docs/specs/database.md` — the drift the design spec called out is fixed. The
  `hooks` / `hook_runs` sections are removed (those tables no longer exist), 15
  missing tables are documented, `agents.agent_type` becomes `profile_id`, the
  `token_ledger` pricing columns are documented, §12 now points at
  `playbook_runs`, and §14 describes Alembic instead of the pre-SQLAlchemy
  `ALTER TABLE` loop. `tests/test_docs_sync.py` now enforces the catalog.

## Deviations from the spec

Recorded rather than silently absorbed:

- **`record_token_usage` call sites unchanged.** The spec expected the existing
  callers to "pass the split/model where their runtime result carries usage".
  `AgentOutput` (`src/models.py:584`) carries only `tokens_used` — no model, no
  split — so both call sites still pass the total alone. The signature is
  extended and tested; the transcript readers from session-runtime become the
  first fully-populated writer, exactly as the spec anticipates.
- **`DoctorRegistry` lives on the orchestrator.** The spec said "handed to
  `CommandHandler` (new ctor kwarg)". `CommandHandler` is constructed in three
  places (`src/api/app.py`, `src/embedded_mcp.py`, `src/runtimes/supervisor.py`)
  and never in `src/main.py`, so a ctor kwarg alone would leave two of the three
  without a registry. The kwarg exists (default `None`, used by tests), and
  `OpsCommandsMixin.doctor_registry` falls back to
  `orchestrator.doctor_registry`, which `src/main.py` sets before
  `initialize()` — early enough for `PluginRegistry` to receive it.
- **`checkout`/`switch`.** §4.2 suggested replacing `["checkout", branch]` with
  `["switch", branch]`. Left as `checkout`: with `_validate_ref` in front, the
  ambiguity `switch` would resolve is already gone, and `switch` would change
  behaviour on older git. `--` separators were added where they are meaningful
  (`diff <ref> --`).
- **`events.registry` is split in two.** The runtime check can only see what a
  live bus observed, which is nothing in a fresh process. The static half —
  every literal `.emit("...")` in `src/` has a schema — is enforced as a test in
  `tests/test_event_schema_registry_validation.py`. It found one real gap:
  `workspace.spec.changed`, now registered.

## Verdict

**PASS** — 2026-08-19, lane 1C (Wave 1). Zero attributable new test failures
(the two candidates are reproducible pre-existing flakes in untouched code,
shown above); the two items
knowingly left open are the golden harness scaffold and the four
subsystem-contributed doctor checks, both blocked on session-runtime /
worktree-execution and both reserved rather than forgotten.
