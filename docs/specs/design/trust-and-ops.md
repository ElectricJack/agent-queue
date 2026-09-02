---
tags: [design, trust, security, ops, doctor, costs, invariants]
---

# Trust Boundaries & Operations

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#2 everything visible, #5 human judgment, #7 events, #10 fewer moving parts)
**Related:** [[workspaces-v2]], [[session-runtime]], [[worktree-execution]], [[aq-surface]], [[feature-pauses]], `docs/analysis/framework-overhaul-todo.md` (Workstream G §10, A.4), `docs/analysis/comparison-gascity-beads.md` (§12.1–12.5)

---

## 1. Purpose & Scope

This spec covers Workstream G of the framework overhaul: the trust model that every
other workstream builds on, session environment scrubbing, the documented permission
posture for agents in worktrees, `aq doctor`, the invariant/docs-sync test suite,
cost accounting (`aq costs`), and the evidence-file convention for substantial changes.

The trust model is modeled on Gas City's `docs/reference/trust-boundaries.md` as
summarized in the comparison doc (§12.3). Everything here is deterministic Python and
tests — zero LLM overhead, per direction decision D2.

Companion implementation spec: `docs/specs/implementation/trust-and-ops.md`.

---

## 2. The Trust Model

Agent Queue runs LLM agents that write code, run shells inside worktrees, and produce
text that flows back through the daemon. The single organizing question for every
piece of text in the system is: **who authored it?**

### 2.1 Trusted: operator code

These sources are authored (or explicitly installed) by the operator and may be
executed, interpolated into commands, and treated as configuration:

| Source | Examples |
|---|---|
| Vault files authored by humans | profiles, harness markdown, workspace-kind markdown, MCP server files, playbooks, project specs |
| `~/.agent-queue/config.yaml` (+ env-profile overlays) | all sections, including `security:` and `pricing:` |
| Shipped defaults in-tree | `src/prompts/`, seeded workspace kinds, default profiles |
| Operator-typed CLI input | `aq` flags and arguments typed by a human |

Vault files are trusted because the vault *is* the operator surface (principle #1:
human-readable files are the source of truth). Installing a vault pack or editing a
workspace kind is the same act as editing config — it is operator code.

### 2.2 Untrusted: data

These are data. They may be stored, displayed, rendered into prompts, and passed as
**values** — but never executed and never interpolated into command strings:

| Source | Examples |
|---|---|
| Task fields | titles, descriptions, acceptance criteria, `close_notes`, `rejection_reason` |
| Chat | Discord/dashboard messages, thread replies, supervisor conversations |
| PR text | titles, bodies, review comments fetched via `gh` |
| Agent output | transcript text, `aq task close --notes`, handoff notes, tool results |
| Anything an agent writes | files in worktrees, commit messages it authored, memory/facts it extracted |
| External content | web pages, MCP tool results, emails |

### 2.3 Trust follows authorship, not location

One nuance the location-based rule misses: **agents write into the vault** (memory
tiers, extracted facts, supervisor-authored specs). Those files live in a trusted
directory but have untrusted authors. The rule is therefore:

- A vault file is trusted as *prompt content* by policy (that is the product — the
  learning loop renders it into context).
- A vault file is trusted as *executable/command text* only for file classes that
  agents never write: config, profiles, harnesses, workspace kinds, MCP definitions.
  Agent-written vault content (memory, facts, specs, notes) is never a source of
  command text, env values that gate behavior, or shell fragments.

Parsers enforce this structurally: exec-capable fields (`worktree_setup`, future
exec hooks) exist only in the schemas of operator file classes.

### 2.4 The rules

| # | Rule |
|---|---|
| R1 | Untrusted text is **never** interpolated into a shell string (`sh -c`, `create_subprocess_shell`). No exceptions, no escaping-based carve-outs. |
| R2 | All subprocess invocations use **argv lists** (`create_subprocess_exec`, `subprocess.run([...])`). |
| R3 | Where a shell is unavoidable — `worktree_setup` in workspace-kind markdown, future exec-style hooks — the command text comes **only from trusted sources** (§2.1). Untrusted values reach such commands via **environment variables or files**, never by string substitution into the command. |
| R4 | Untrusted text as an argv **flag value** (`-m <msg>`, `--title <t>`) is acceptable, but positional untrusted values must be guarded against **argument injection**: pass `--` separators where git supports them, and validate refnames before use (`git check-ref-format` semantics; reject leading `-`). |
| R5 | Rendering untrusted text into prompts, Discord embeds, dashboards, and logs is normal operation. The boundary is execution, not display. |
| R6 | Subprocesses launched for agent sessions receive a **scrubbed environment** (§3), never the raw daemon environment. |

### 2.5 Current-state audit (2026-08-19)

Findings from reading the code; remediation is itemized in the implementation spec.

| Finding | Location | Verdict |
|---|---|---|
| All git invocations are argv lists via `_run` / `_arun_unlocked` / `_arun_subprocess`; no `sh -c` anywhere in `GitManager` | `src/git/manager.py:126,168,212` | **Compliant** with R1/R2 |
| Commit messages, PR titles/bodies pass as flag values (`["commit","-m",message]`, `gh pr create --title … --body …`) | `src/git/manager.py:1599,1648` | Compliant (R4 flag-value case) |
| Branch names reach git as **positional** args (`acheckout_branch`, `aswitch_to_branch`, `adelete_branch`, `apush_branch`, …). System-generated names are safe today, but `base_branch` can arrive from task metadata; a name starting with `-` becomes an option | `src/git/manager.py` (branch APIs) | **Remediate**: refname validation + `--` separators |
| `_run_subprocess_shell` runs an arbitrary string via `/bin/sh -c`; sole caller is `_cmd_run_command`, whose `command` argument is authored by the chat/supervisor LLM — untrusted per §2.2. It is already excluded from MCP (`run_command` in `DEFAULT_EXCLUDED_COMMANDS`) and sandboxed to allowed working dirs, but it executes on the daemon host with the daemon's env | `src/commands/helpers.py:127`, `src/commands/system_commands.py:690`, `src/mcp_registration.py:51` | **Known R1 violation, contained**. Interim: scrubbed env + keep MCP-excluded. It is slated to disappear with the in-process supervisor chat loop (overhaul D2); agents get shells inside worktrees instead |
| Git/`gh` subprocesses inherit `**os.environ` plus prompt-disabling vars | `src/git/manager.py:90` | Acceptable (daemon-side tool, not an agent session), revisit when worktree-execution centralizes git env |
| Agent subprocess env strips only `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` | `src/runtimes/_subprocess.py:22` | **Remediated (lane 1C)**: `isolated_env` delegates to `scrub_env`, and `RuntimeRegistry.create` hands the daemon `AppConfig` to `ACPXRuntime` so `security.env_scrub_enabled` / `env_allowlist` are read at the real launch site |
| The **default** runtime (`claude_sdk`) is not scrubbed: the Claude Agent SDK builds its child env as `{**os.environ, **options.env}`, so `options.env` can override a key but cannot remove one. The adapter pops `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` from the daemon's own `os.environ` for the same reason | `src/runtimes/claude_sdk.py` (`wait`) | **Open gap, recorded not fixed.** R6 today covers `acpx` and `run_command` only. Closing it needs the spawn owned by [[session-runtime]] (it builds the child env itself and calls `scrub_env`); widening `options.env` is not a fix — setting a credential to the empty string is a different and worse failure than withholding it |

---

## 3. Session Environment Scrubbing

Agent sessions run arbitrary code. The daemon's environment carries the Discord bot
token, database DSNs, embedding API keys, and whatever else the operator's shell
exports. None of that belongs in an agent's environment by default.

**Rule:** every agent session env starts from a **scrubbed copy of the daemon env**.
A key is dropped when its name — upper-cased, with `-` normalised to `_` — contains
any of:

```
TOKEN · API_KEY · APIKEY · SECRET · PASSWORD · PASSPHRASE · CREDENTIAL
PRIVATE · AUTH · DSN · WEBHOOK · NETRC · KUBECONFIG
```

or matches one of the anchored patterns `(^|_)KEY$`, `(^|_)PAT$`, `(^|_)ID_RSA`,
`(^|_)ID_ED25519` — anchored because a bare `KEY` substring also matches
`KEYBOARD_LAYOUT` and a bare `_PAT` also matches `LD_LIBRARY_PATH`. A key is also
dropped when its **value** is a credential-bearing URI (`scheme://user:pass@host`),
which is how `DATABASE_URL=postgres://user:password@host/db` — named in this
section as something that must not leak — is caught despite an innocent name.
Values are inspected but never logged, returned, or included in an error.

**This denylist is best-effort, not complete.** It cannot enumerate every
secret-shaped name an operator's shell might export, and §2.5 argues (correctly)
that a substring blocklist over *shell command text* is theater. The difference is
the input, not the technique: env var names come from the operator's own shell and
the daemon's own config, never from an adversary choosing names to evade the
filter. Against that input, denylist-plus-allowlist is the pragmatic control; the
guarantee is "the daemon's known secrets are withheld", not "no secret can pass".

| Layer | Behavior |
|---|---|
| Built-in exemptions | `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_AUTHOR_DATE` — false positives of the `AUTH` pattern; shipped in code, visible in the spec |
| Default harness credentials | `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_*`, `GEMINI_*`, `GOOGLE_API_KEY`, `GH_TOKEN`/`GITHUB_TOKEN` and the other vendor prefixes in `HARNESS_CREDENTIAL_ALLOWLIST` — see the decision below |
| `security.env_allowlist` (config.yaml) | operator-listed names or globs that pass through unscathed |
| Harness / profile `env` maps | **explicit values always win** — setting a key in a harness or profile env injects it regardless of patterns; explicitness is operator intent |
| `AQ_*` session markers | injected by the session builder after scrubbing (`AQ_SESSION_ID`, `AQ_TASK_ID`, `AQ_API_URL`, …) |
| `AQ_API_TOKEN` | **explicitly injected**; minting and scoping of the task-scoped token is owned by [[aq-surface]] — the scrubber only guarantees the daemon's own secrets don't leak alongside it |

`CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` (`STRIP_ALWAYS`) are removed regardless of
the patterns *and* regardless of the kill switch — they exist to stop an
**inherited** marker convincing a nested CLI it is already in a session. An
`explicit` entry naming one of them still wins: that is an operator saying
otherwise, and stated intent outranks an inherited value.

**Decision: the scrub ships default-on with a shipped credential allowlist**
(rather than defaulting `env_scrub_enabled` to `False`). An agent CLI that cannot
authenticate is not a safer agent, it is a broken install — and API-key auth is the
normal install shape, since the setup wizard writes `ANTHROPIC_API_KEY` into the
daemon env file. The value of the scrub is withholding the daemon's *own* secrets —
messaging bot token, database DSN, embedding keys, the operator's unrelated
exports — and that survives the allowlist intact. Entries are vendor-prefix globs
rather than an exact key list because `acpx` fans out to 14+ agents and a new
one's key name must not silently break it. An operator wanting a harder lockdown
turns the defaults off and names exact keys in `security.env_allowlist`; the
`run_command` shell already does exactly that (it is not a harness, so it gets no
vendor credentials).

**Where it applies today.** `isolated_env` (the `acpx` runtime) and
`_cmd_run_command`. It does **not** apply to the default `claude_sdk` runtime —
see the §2.5 row: the Agent SDK merges `options.env` over a full `os.environ` copy
and offers no way to remove an inherited key. R6 is therefore partially enforced,
and this document says so rather than implying otherwise.

The scrub is one pure function (`scrub_env`) owned by this workstream. Today's
`isolated_env()` in `src/runtimes/_subprocess.py` becomes a thin wrapper over it;
[[session-runtime]]'s `SessionSpec` builder consumes the same function, so the policy
survives the runtime replacement. Scrub results are auditable: the function returns
the dropped key names (names only, never values) so `aq doctor` and debug logs can
show what was withheld.

---

## 4. Permission Posture: Skip-Permissions Inside Worktrees

**Documented default:** task sessions run their harness with permission prompting
disabled (Claude: `--dangerously-skip-permissions`; equivalents per harness) when —
and only when — the session's `work_dir` is an isolated per-task worktree
([[worktree-execution]]).

**Reasoning.** A permission prompt is a question addressed to a human. In a detached
tmux pane there is no human; an unanswered prompt is an indefinite stall, and
auto-answering prompts from pane scraping is strictly worse than not asking. Gas City
runs every harness this way for the same reason. The safety property does not come
from per-call confirmation; it comes from the **boundary**:

| Boundary element | What it guarantees |
|---|---|
| Isolated worktree + fresh `aq/<task>` branch | writes land in a disposable tree; the durable artifact is a branch that must pass the merge slot, gates, and review before integration |
| Scrubbed env (§3) | the agent cannot exfiltrate daemon credentials it was never given |
| Task-scoped `AQ_API_TOKEN` ([[aq-surface]]) | the agent's authority over the orchestrator is its own task's surface, not the admin API |
| Git as recovery | every change is diffable, revertable, and attributable to the session |

**Honest limits.** Skip-permissions does not confine the *process*: an agent can read
world-readable paths on the host and reach the network. Filesystem/network sandboxing
(containers, landlock) is explicitly out of scope for this phase — it would
contradict "fewer moving parts" before the session runtime is stable — but the trust
doc is the place where that gap is written down rather than implied away. Sessions
outside an isolated worktree (shared-workspace tasks, the daemon-host shell in §2.5)
do **not** get skip-permissions by default; profiles must opt in.

---

## 5. `aq doctor`

One entry point that answers "is this install healthy, and what should I do about
it?" — replacing knowledge scattered across `/health`, ad-hoc commands, and log
spelunking. Modeled on `gc doctor` / `bd doctor` (comparison §12.1).

### 5.1 Shape

Doctor is a CommandHandler command (`_cmd_doctor`), surfaced as `aq doctor [--fix]
[--json]`, as an MCP tool, and to the dashboard via the API. Every check returns:

```json
{"id": "db.wal_size", "severity": "warn", "detail": "WAL is 210 MB (threshold 64 MB)",
 "fixable": true, "fix_applied": false}
```

`severity ∈ ok | info | warn | error`. Checks run concurrently with per-check
timeouts; a check that crashes or times out reports `error` with the exception —
doctor never hangs and never dies on one bad check.

### 5.2 Check catalog

| id | What it verifies | Severity on failure | Fixable |
|---|---|---|---|
| `config.parse` | `config.yaml` loads and `AppConfig.validate()` is clean | error (errors) / warn (warnings) | no |
| `db.connect` | database reachable (trivial query) | error | no |
| `db.migrations` | Alembic revision at script head | error | no (prints `alembic upgrade head`) |
| `vault.parse` | profiles, harnesses, workspace kinds, MCP files parse | error per broken file | no |
| `harness.binaries` | required binaries respond. **As landed:** `git` required; `gh`, `claude`, `acpx` optional. Narrowed from "per configured harness" — deriving the set means mapping every active profile's `runtime`/`agent_name` to a binary, and that mapping lives in `acpx`, not here | error (`git`) / warn (optional) | no |
| `tmux.server` | tmux socket probe (contributed by [[session-runtime]]) | error when sessions enabled; info otherwise | no |
| `sessions.stale` | session rows vs process table (contributed by [[session-runtime]]) | warn | yes — reconcile rows through the exit classifier |
| `worktrees.orphans` | orphan worktree dirs, stale `.git/worktrees` entries (contributed by [[worktree-execution]]). **As landed:** slot worktrees whose `.aq-worktree.json` names a task no longer in `tasks` — a released slot stays on its last task's branch (worktree-execution §3.4), so a deleted task leaves `aq/<task_id>` checked out and git refuses it to every other slot. Report-only: `git worktree prune` does not clear a live worktree's own checkout, and resetting a slot off a branch is an operator call | warn | no — see "as landed" |
| `leases.stale` | leases past TTL with no live session | warn | yes — clear lease, task re-enters stall handling |
| `workspaces.base_sessions` | live sessions whose `work_dir` is a **base** workspace — the clone that hosts a kind's slot worktrees, routinely a human's own checkout. Registered unconditionally by the core registry; the launch-time half of the rule is the refusal in `src/orchestrator/base_workspace.py`, which a profile opts out of with `allow_base_checkout: true` | error | no — stopping a session is an operator call |
| `profiles.system_drift` | each vault copy of a shipped system profile (ids under `src/profiles/defaults/`) still matches the shipped default on the semantic `## Config` fields (`read_only`, `harness`, `lifecycle`, `needs_workspace`) and has not lost a section. `ensure_default_profiles()` is write-if-absent, so an old vault copy keeps old semantics forever — a stale `read_only: false` on `reviewer` re-arms the require-a-PR close gate in `git_ops._task_produces_no_code()`. Cosmetic config keys and operator-added sections are not drift | warn (divergence) / error (vault copy no longer parses) | no — overwriting would discard operator edits; repair with `aq agent profile-reseed <id>` |
| `db.wal_size` | SQLite WAL above threshold | warn | yes — `PRAGMA wal_checkpoint(TRUNCATE)` |
| `logs.llm_size` | `logs/llm/` size / dirs older than retention | warn | yes — `LLMLogger.cleanup_old_logs()` (enforces configured retention) |
| `tasks.stuck` | tasks past `monitoring.stuck_task_threshold_seconds` | warn | no |
| `pauses.active` | paused subsystems (memory, playbooks, orchestrator) — from [[feature-pauses]] flags | **info** (pauses are intentional) | no |
| `events.registry` | every event type the live `EventBus` has **actually dispatched** (`EventBus.seen_event_types`) has a registered payload schema. Reports INFO, not OK, when nothing has been emitted yet — "nothing was looked at" must not read like "nothing is wrong". The complementary static half (every literal `.emit("…")` in `src/` has a schema) is a test, not a runtime check | warn | no |
| `mcp.probes` | configured MCP servers respond to probe (10 s timeout) | warn | no |
| `plugin.<name>.<id>` | plugin-contributed checks via `PluginContext` | per check | per check |

### 5.3 Severity policy

- **error** — the daemon cannot operate correctly or data integrity is at risk
  (unparseable config, unreachable DB, schema behind head, broken vault file in use).
- **warn** — degraded or heading toward a problem; operator action recommended but
  nothing is currently wrong enough to stop work.
- **info** — intentional state worth surfacing (paused subsystems, tmux absent on an
  install that doesn't use sessions). Never fails CI.
- **ok** — check passed; included in output so the catalog is visible.

### 5.4 `--fix` safety rules

A fix may be applied automatically only if it is **idempotent** (safe to run twice)
and **non-destructive to primary data** — it either enforces already-configured
policy (log retention) or cleans derived/stale state (WAL, stale git registrations,
dead session rows, expired leases). Fixes never delete tasks, vault files, branches,
or worktree directories that contain content; those always remain human decisions
(principle #5) — which is why `profiles.system_drift` ships without a fix: the
vault profile is an operator-owned file, so its repair is the explicit,
per-profile `aq agent profile-reseed` (backs the old file up to
`profile.md.bak-<epoch>` first). Fixable checks: `sessions.stale`, `worktrees.orphans` (prune only),
`leases.stale`, `db.wal_size`, `logs.llm_size`, plus plugin checks that declare a fix
meeting the same rules. `--fix` re-runs each fixed check and reports the post-fix
severity with `fix_applied: true`.

### 5.5 Contributed checks

Doctor owns the runner and the generic checks; **subsystems own their own checks**
and register them at startup through the same registry plugins use
(`PluginContext.register_doctor_check` for plugins; direct registry access for core
subsystems). Session/worktree/lease checks consume state owned by [[session-runtime]]
and [[worktree-execution]]; pause reporting consumes [[feature-pauses]] flags. Doctor
never reaches into another subsystem's internals — it calls the probe the owner
registered (principle #8).

### 5.6 Exit codes (CI use)

| Code | Meaning |
|---|---|
| 0 | all checks ok or info |
| 1 | at least one warn, no errors |
| 2 | at least one error |
| 3 | doctor itself failed to run |

`aq doctor --json` emits the full result set for machine consumption; CI gates on
exit code.

A `--check <id>` filter naming an id that is neither registered nor reserved is an
**error result** for that id (exit 2), not an empty table with exit 0. A CI gate
pinned to a misspelled check id must fail loudly; silently passing is the worst
possible answer for a health command.

---

## 6. Invariant & Docs-Sync Tests

Cheap tests that catch drift between code, docs, and registries (comparison §12.2 —
several such drifts were found during the review, e.g. `docs/specs/database.md` still
describes the pre-SQLAlchemy layer). The suite:

| Invariant | Enforcement |
|---|---|
| Every table in `src/database/tables.py` appears in `docs/specs/database.md` (and vice versa) | parse doc for table names, compare against `metadata.tables`, small explicit exclusion list (`alembic_version`) |
| Every `_cmd_*` on `CommandHandler` is either MCP-registered (explicit in `_ALL_TOOL_DEFINITIONS` or intentionally auto-discovered) or in the exclusion list | introspection test; new commands must be placed deliberately |
| Every emitted event type has a registered payload schema | extends the existing `test_event_schema_registry_validation.py` / `test_emit_schema_compliance.py` coverage to assert registry completeness against emit call sites |
| State-machine enforcement flag honored | when strict mode is on, illegal `transition_task` raises; `force=True` bypasses (lands with Workstream D; test asserts the flag's contract) |
| Harness profile goldens | each shipped `vault/harnesses/*.md` parses to a golden `SessionSpec` (command argv, env, ready config); lands with [[session-runtime]], shape specced now |

These run in the normal `pytest tests/ -n auto` suite — no separate CI job, no
tooling beyond pytest.

---

## 7. Cost Accounting — `aq costs`

`token_ledger` already records per-project/agent/task token counts; pricing turns
counts into money so reflection/automation spend is visible (comparison §12.4).

**Config** (`config.yaml`):

```yaml
pricing:
  - {model: "claude-sonnet-4-5*", input_per_mtok: 3.00, output_per_mtok: 15.00}
  - {model: "claude-haiku-*",     input_per_mtok: 1.00, output_per_mtok: 5.00}
```

Entries match in order; `model` supports globs. Prices are per **million** tokens.

**Aggregation** — `aq costs [--project] [--since]` rolls up `token_ledger` by
project, profile (via `agents.profile_id`), and day. Honesty rule: the ledger today
stores only `tokens_used` totals with no model or input/output split, so historical
rows cannot be priced accurately. The ledger gains nullable `model`, `input_tokens`,
`output_tokens` columns; new writers (and the transcript readers from
[[session-runtime]] A.6) populate them. Rows without a split or without a matching
pricing entry are reported as `unpriced_tokens` — never silently priced at a guessed
rate. Cost = `input_tokens × input_per_mtok / 1e6 + output_tokens × output_per_mtok / 1e6`.

The rule holds **within** a row as well as across rows. The rollup buckets by
`(group, model)`, so one bucket can hold both split and unsplit ledger entries;
pricing it off the split sum alone would leave the unsplit tokens counted in
neither `cost_usd` nor `unpriced_tokens`. Each row therefore carries its own
`unpriced_tokens = tokens_used − (input_tokens + output_tokens)` when priced, and
its whole `tokens_used` when not. The invariant every reader can rely on: for each
row, *priced tokens + unpriced tokens = tokens_used*.

**Status as landed (lane 1C):** the read path is complete but there is no
fully-populated **writer** yet. `AgentOutput` (`src/models.py`) carries only a
`tokens_used` total — no model, no split — so both existing call sites
(`src/orchestrator/execution.py`, `src/orchestrator/sync_workflow.py`) still record
totals alone. Every row is therefore unpriced and `total_cost_usd` is `0.0` on a
real install. `aq costs` is honest about this rather than wrong; the transcript
readers from [[session-runtime]] are the first writer that populates model + split.

---

## 8. Evidence Files — `docs/gates/<change>.md`

For substantial changes (new subsystem, schema change, behavior change with rollback
risk), the author writes a lightweight evidence file before merge:

```markdown
---
tags: [gate]
---
# <change name>
## Acceptance criteria   — what "done" was defined as, up front
## Test evidence         — commands run, suites passed, manual checks
## Spec diff             — which specs were updated (specs first, then code)
## Verdict               — PASS / FAIL, date, author
```

This is a **convention only** — no tooling, no doctor check, no CI gate. It extends
the existing specs-first rule with a per-change record (comparison §12.5, Gas City
`release-gates/`). If the convention proves valuable, tooling can follow; if not, it
cost nothing.

---

## 9. Ownership & Cross-References

| Concern | Owner | This spec's relationship |
|---|---|---|
| trust model, env scrub function, doctor runner + generic checks, invariant tests, costs, evidence convention | **this spec** | — |
| session rows, tmux probe, exit classifier, transcript token data | [[session-runtime]] | doctor consumes registered checks; scrub function consumed by SessionSpec builder |
| worktree lifecycle, orphan detection, `git worktree prune` | [[worktree-execution]] | doctor consumes registered checks |
| pause flags (`memory.enabled`, `playbooks.enabled`) | [[feature-pauses]] | doctor reports them as info |
| `AQ_API_TOKEN` minting, scoping, revocation | [[aq-surface]] | scrubber injects the token it is handed |

## 10. Non-Goals

- Process sandboxing (containers, seccomp, landlock) — written down as a gap (§4),
  not solved here.
- Secrets management/rotation — the scrub prevents leakage of daemon env; it is not
  a vault for agent credentials.
- Automated enforcement of evidence files.
- Pricing precision for historical ledger rows (reported unpriced instead).
