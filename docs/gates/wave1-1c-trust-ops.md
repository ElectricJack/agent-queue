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

## Adversarial review round (2026-08-19)

An adversarial review of the completed, unmerged lane reproduced every finding
below by execution. Fixes are in this branch; the entries are kept so the record
shows what shipped broken and what the correction was.

### Blocking

| # | Finding | Fix |
|---|---|---|
| A1 | **The kill switch and allowlist were inert.** `ACPXRuntime.wait` called `isolated_env()` with no `config`, and the runtime had no config to give — `isolated_env` only reads `security.*` when handed one. The test that "proved" the switch worked called `isolated_env(config=…)` directly, a path with zero production callers. Consequence on merge: every ACPX agent would lose `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, with `env_scrub_enabled: false` powerless to recover | Config now flows `main.py` → `default_registry(config=…)` → `RuntimeRegistry.create` → `ACPXRuntime(config=…)` → `isolated_env(config=self._config)`. `tests/test_env_scrub.py::TestAcpxRuntimeHonoursTheConfig` drives `RuntimeRegistry.create` and a stubbed subprocess — the **real** call site — for the kill switch, the operator allowlist and credential survival |
| A1b | **Design question: default-on scrub vs. a working install.** | **Chose (a): default-on plus a shipped `HARNESS_CREDENTIAL_ALLOWLIST`.** Reasoning in design §3. An agent CLI that cannot authenticate is a broken install, not a safer one, and the setup wizard writes `ANTHROPIC_API_KEY` into the daemon env file, so API-key auth is the normal shape. Defaulting the switch off (option b) would have made the lane's headline control ship dead, which is the same failure as A1 with better documentation. The scrub's actual value — withholding the bot token, database DSN, embedding keys and the operator's unrelated exports — survives the allowlist untouched. Entries are vendor-prefix globs (`ANTHROPIC_*`, `OPENAI_*`, …) because `acpx` fans out to 14+ agents; `run_command` opts out via `harness_credentials=False` since a diagnostic shell is not a harness |
| A2 | **Merging would break the build.** Lane 1D (already on `main`) added `_cmd_get_schema` / `_cmd_task_show` / `_cmd_task_set`; none were placed, so `test_every_command_is_placed_deliberately` fails post-merge | Typed definitions added for all three (the preferred branch of the fix — they are first-class agent surface). `get_schema` → `system` category; `task_show` / `task_set` → the existing `task` category, so they load on demand and the hand-crafted `aq task show` / `aq task set` win the CLI name collision. Verified by a scratch merge against `main` |

### Correctness

| # | Finding | Fix |
|---|---|---|
| B1 | R6 was **not** enforced for `claude_sdk`, the default runtime, while `src/env_scrub.py` claimed "every subprocess … starts from a scrubbed copy" | **Not fixable cleanly, so recorded.** The Agent SDK builds its child env as `{**os.environ, **options.env}`; `options.env` can override a key but cannot remove one, and setting a credential to `""` is a worse failure than withholding it. The overstated docstrings are corrected, the gap is a row in design §2.5, an unchecked item in the implementation §7 checklist, and a comment at the `os.environ.pop` site. It closes when session-runtime owns the spawn |
| B2 | The denylist missed the spec's own named example: `DATABASE_URL=postgres://user:password@host/db` passed straight through, as did `PG_DSN`, `SENTRY_DSN`, `SLACK_WEBHOOK_URL`, `APIKEY`, `GH_PAT`, `SSH_KEY`, `ID_RSA`, `PASSPHRASE`, `NETRC`, `KUBECONFIG` | Patterns extended (`APIKEY`, `PASSPHRASE`, `DSN`, `WEBHOOK`, `NETRC`, `KUBECONFIG`, `PRIVATE` superseding `PRIVATE_KEY`), plus anchored regexes `(^\|_)KEY$` / `(^\|_)PAT$` / `(^\|_)ID_RSA` — anchored because bare `KEY` also matches `KEYBOARD_LAYOUT` and bare `_PAT` also matches `LD_LIBRARY_PATH`, both now pinned as near-miss tests. Names are normalised (`-`→`_`) so `API-KEY` matches. A value that is a credential-bearing URI (`scheme://user:pass@host`) is dropped whatever the name — that is what catches `DATABASE_URL`; values are never logged or returned. Design §3 now states plainly that the denylist is **best-effort, not complete**, and why a substring denylist is the right call *here* (operator-authored names) while §2.5 rejects one over LLM-authored shell text (adversary-chosen) |
| C1 | `_cmd_run_command` containment was claimed but not held: `POST /api/execute {"command":"run_command"}` reached `_run_subprocess_shell`; `API_EXCLUDED` didn't list it and `/api/execute` didn't consult `API_EXCLUDED` at all | `run_command` added to `API_EXCLUDED`, and `/api/execute` now returns 403 for anything in that set (it was a back door around the typed routes for `load_tools` / `send_message` / `reply_to_user` too). Both docstrings rewritten to name the three remote gates — MCP, CLI, API — and to say plainly that in-process callers are deliberately **not** gated |
| C2 | The `events.registry` check read `bus.seen_event_types` / `bus._seen_event_types`; neither existed anywhere in `src/`. The only definition was the fake bus in the test — **the test invented the API it validated**, and on a real install the check always reported OK | `EventBus.emit` now records dispatched types and `EventBus.seen_event_types` exposes a copy. The check reads the real attribute and reports **INFO** ("nothing emitted yet"), never OK, when it has observed nothing — "nothing was looked at" must not read like "nothing is wrong". Every case in `TestEventsRegistryCheck` now drives a real `EventBus` |
| C3 | Cost rollup silently dropped tokens: buckets are `(group, model)`, `has_split` was computed per bucket, so a bucket mixing split and unsplit rows was priced on the split sum and the unsplit tokens counted toward neither `cost_usd` nor `unpriced_tokens` | Each row now reports its own `unpriced_tokens` (`tokens_used − split` when priced, all of `tokens_used` when not) and those sum into the total. `tests/test_costs.py` asserts the identity *priced + unpriced = tokens_used* per row |

### Smaller, verified

| # | Finding | Fix |
|---|---|---|
| D2 | `scrub_env({"CLAUDECODE":"1"}, explicit={"CLAUDECODE":"1"})` returns it, contradicting "removed regardless" | **Docstring and spec corrected; behaviour kept.** `STRIP_ALWAYS` exists to stop an *inherited* marker fooling a nested CLI; an operator naming the key in a harness/profile `env` map is stated intent and outranks inheritance. Pinned by a named test so the reading can't drift back |
| D3 | `aq doctor --check typo.id` exited **0** with an empty table — a CI gate on a misspelled id passed silently | An `only` entry matching neither a registered check nor a reserved id now yields an ERROR result for that id → exit 2. Four tests, including "one bad id among good ones" |
| D5 | `by_id[r.id]` could `KeyError` under `--fix` when a check returns a `CheckResult` with a different id (plugin checks) | Defensive lookup in the runner, plus `try/except` around `run_doctor` in `_cmd_doctor` — doctor is what an operator reaches for when things are already broken |
| D6 | `_probe_binary` cancelled `communicate()` on timeout but never killed the child — process leak | `_terminate()` kills and reaps on both the timeout and the error path; the probe timeout moved to `_PROBE_TIMEOUT_S` so the test can drive it without a 5 s sleep |
| D9 | The shell-helper invariant allow-listed whole *files*, so a second `_run_subprocess_shell` caller inside `system_commands.py` would pass | Rewritten to count call sites: exactly one definition (in `helpers.py`) and exactly one call (in `system_commands.py`) |
| C6 | `_validate_ref` rejected revision expressions, so `git_diff(base_branch="HEAD~1")` returned an error dict while `vibecop`'s schema advertises `HEAD~3` | Added `_validate_rev()` for the **read-only** diff APIs (`aget_diff`, `aget_changed_files`): same alphanumeric anchor, so a leading `-` is still impossible, widened by exactly git's revision suffixes (`~ ^ @ { }`). Write paths keep the stricter guard — pinned by a test. Both git-plugin schemas now document the accepted forms |

### Recorded, not fixed

| # | Finding | Record |
|---|---|---|
| C4 | `aq costs` is **inert**: no writer populates `model` / `input_tokens` / `output_tokens`, so every row is unpriced and `total_cost_usd` is always `0.0` | Now stated in design §7 ("Status as landed") and as an **unchecked** item in implementation §7. The read path is complete and tested; the command is honest, not yet useful |
| C5 | `harness.binaries` hardcoded `required=["git"], optional=["gh"]` while design §5.2 says "per configured harness" and names `claude --version` | Partially widened — `claude` and `acpx` (the two shipped runtime front-ends) are now probed as optional. Deriving the set from active profiles is **not** implemented: the profile→binary mapping for the 14+ ACP agents lives in `acpx`. The narrowing is written into design §5.2 and implementation §5.3 |

### Reviewer findings this lane did *not* change

The reviewer verified as sound, and these were left alone: `os.environ` is never
mutated by the scrub; no value can leak into `dropped`, logs or exceptions;
`explicit` wins; the `GIT_AUTHOR_*` exemption is exact-match; no allowlist glob
bypass; `_validate_ref` raises before any git spawn; `--fix` obeys §5.4 and is
idempotent; exit codes; per-check timeouts and crash isolation; the
`DoctorRegistry` three-site wiring; the `database.md` rewrite.

### Test evidence for the review round

```
tests/test_env_scrub.py          all passed (was 46, now covers the real ACPX call site)
tests/test_doctor.py             all passed (77)
tests/test_costs.py              all passed
tests/test_command_surface.py    all passed
tests/test_git_manager_async.py  110 passed (was 82)
tests/test_docs_sync.py          all passed
tests/test_event_bus*.py         all passed
tests/test_runtimes_acpx.py, test_runtimes_registry.py   all passed
```

`tests/test_tool_registry.py` (5 failures), `tests/test_cli.py` (2) and
`tests/test_runtimes_subprocess.py` (2) fail identically with and without these
changes — verified by stashing the diff and re-running. The `test_tool_registry`
ones are the drift `main` already fixed in its own copy of that file.

**Merge simulated**, not assumed. A scratch worktree at `main` (`534f6332`, i.e.
with lanes 1B and 1D already merged) took `git merge wave1/1c-trust-ops` with
zero conflicts, and against that merged tree:

```
tests/test_command_surface.py  tests/test_surface_commands.py
tests/test_env_scrub.py        tests/test_doctor.py
tests/test_costs.py            tests/test_docs_sync.py     → 254 passed

tests/test_tool_registry.py    tests/test_cli.py       tests/test_cli_envelope.py
tests/test_mcp_server.py       tests/test_event_bus.py tests/test_runtimes_acpx.py
tests/test_git_manager_async.py                        → 386 passed, 2 failed
```

The two are `test_cli.py::TestAutoCommands::{test_category_groups_exist,
test_auto_command_help}`, which fail identically on **plain `main`** with this
branch nowhere in sight (re-run to confirm). `test_tool_registry.py`, which fails
on this branch alone, passes post-merge — those five failures were the drift
`main` fixed.

**Known deliberate exception:** `PENDING_ON_ANOTHER_LANE` in
`tests/test_command_surface.py`. The three lane-1D definitions have no `_cmd_*`
method *on this branch*, so `test_tool_definitions_have_no_orphans` would fire
here even though the definitions are exactly what keeps the merge green. The set
excludes them from that one check and is documented for deletion once both lanes
are on `main` — at which point removing it is a no-op, because the methods exist.

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
  live bus observed. The static half — every literal `.emit("...")` in `src/` has
  a schema — is enforced as a test in
  `tests/test_event_schema_registry_validation.py`. It found one real gap:
  `workspace.spec.changed`, now registered. (The runtime half originally read an
  attribute that did not exist; see C2 above. It now reads a real
  `EventBus.seen_event_types` and reports INFO when it has observed nothing.)

## Verdict

**PASS with corrections** — 2026-08-19, lane 1C (Wave 1), after the adversarial
review round recorded above.

The first PASS was wrong on two counts and this file now says so: the env-scrub
kill switch and allowlist were unreachable from production (A1), and merging
would have broken `tests/test_command_surface.py` against `main` (A2). Both are
fixed and pinned by tests that exercise the real call sites.

Zero attributable new test failures; the pre-existing flakes and the three suites
that fail identically with and without this branch are itemised above.

**Knowingly open on merge**, each written into the specs rather than implied
away:

1. R6 does not cover the default `claude_sdk` runtime (B1) — blocked on
   session-runtime owning the spawn.
2. `aq costs` has no fully-populated writer, so every row is unpriced (C4) —
   blocked on `AgentOutput` carrying a model and a token split.
3. `harness.binaries` probes a fixed list rather than the configured harnesses
   (C5).
4. The golden harness scaffold and the four subsystem-contributed doctor checks —
   blocked on session-runtime / worktree-execution, reserved rather than
   forgotten.
