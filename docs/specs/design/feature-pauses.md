---
tags: [design, feature-pauses, memory, playbooks, overhaul, feature-flags]
---

# Feature Pauses — Memory & Playbooks Off While the Core Is Retuned

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#2 everything is visible and editable, #5 reduce
human effort not judgment, #10 favor fewer moving parts)
**Related:** `docs/analysis/framework-overhaul-todo.md` (§0 D3/D4, §7 Workstream E, §8
Workstream P), [[self-improvement]], [[playbooks]], [[memory-plugin]], [[memory-scoping]],
`docs/specs/implementation/feature-pauses.md` (the paired implementation spec)

---

## 1. Problem & Direction

The framework overhaul (todo v3, decisions D3/D4) retunes the core of agent-queue — session
runtime, worktrees, supervisor-as-agent, `aq` surface — and needs the two most ambitious
subsystems out of the signal path while that happens:

- **Memory** (D4 — *pause, don't strip*): the aq-memory plugin, L1/L2 prompt tiers,
  reflection, and consolidation are the product's differentiator, but they add LLM cost,
  latency, and confounding variables to every task while the execution core is being
  rebuilt. They come back through the new `aq prime` delivery path once the core is stable.
- **Playbooks** (D3 — *pause, don't replace*): the concept has merit but is half-baked and
  under-tested. The comeback design (orders + formulas + judgment tasks, todo §8) is a
  candidate, not current work.

This spec defines what "paused" means, the switch that implements it, and the exhaustive
list of behaviors that change. It deliberately does **not** redesign either subsystem — the
existing [[self-improvement]] and [[playbooks]] specs remain the design of record for the
frozen code and are annotated as paused, not superseded.

---

## 2. The Pause Contract

A paused subsystem satisfies four guarantees, in priority order:

1. **Feature-flag off.** One boolean in `~/.agent-queue/config.yaml` is the single source
   of truth. Nothing else (env var, DB row, vault file) can independently re-enable the
   subsystem.
2. **Code frozen.** The modules stay in-tree and importable. Bug fixes only — no new
   features, no refactors that aren't required by a bug fix. Their tests keep running
   (see §8).
3. **Data preserved untouched.** No rows deleted, no files removed, no migrations that drop
   or rewrite subsystem data. Vault markdown stays human-editable throughout (principle #2).
4. **Clean re-enable path.** Un-pausing is a config flip plus a daemon restart. By design
   there are **no migrations on re-enable** — the pause must never leave data in a shape
   the un-paused code cannot read.

"Paused" is therefore stronger than "disabled by default" and weaker than "removed": the
subsystem is inert at runtime but fully recoverable, and an operator can always inspect its
state on disk.

---

## 3. The Pause Switch

### 3.1 Two flags, defaults flipped

```yaml
# ~/.agent-queue/config.yaml
memory:
  enabled: false      # default flips from true → false in this overhaul
playbooks:
  enabled: false      # new section; default false
```

- `memory.enabled` already exists on `MemoryConfig` (`src/config.py`) but — verified — has
  **no consumer in `src/` today**; this spec gives it teeth and flips its default.
- `playbooks.enabled` is new: a `PlaybooksConfig` dataclass with the single field. The
  existing top-level `max_daily_playbook_tokens` / `max_concurrent_playbook_runs` knobs are
  left where they are (freeze means minimal churn).
- Both flags are **restart-required**, never hot-reloaded. A paused subsystem was never
  constructed; a hot flip could not conjure it. `memory` is already in
  `RESTART_REQUIRED_SECTIONS`; `playbooks` joins it.
- Both flags are **documented as temporary** — the config comments and `docs/specs/config.md`
  must say they exist for the overhaul window and point at the comeback plan (todo §7/§8).

### 3.2 Constructor-level "don't start", not per-call checks

The preferred mechanism is to **not construct or wire** a paused component at startup,
rather than sprinkling `if enabled` checks through its call paths:

- If `PlaybookManager` is never created, then trigger subscription, vault-watcher
  registration, compilation reconcile, and the timer map are all absent by construction.
- If the aq-memory plugin is never loaded, then the `"memory"` plugin service is never
  registered, and every existing `mem_svc = plugin_registry.get_service("memory")` guard in
  the codebase (task execution, supervisor prompt, facts watcher wiring) already degrades
  to "skip" — those call sites need **zero changes**.

Per-call checks are used only where a component cannot simply be absent: the command gate
(§3.3), early returns in two housekeeping methods whose *callers* stay alive, and the
reflection level override. Every such check reads the flag from `AppConfig` — never a copy.

### 3.3 One gate covers four surfaces

All four command surfaces — Discord, MCP tools, `aq` CLI, and the HTTP API/dashboard —
dispatch through `CommandHandler.execute()` (verified: `src/embedded_mcp.py` shares the
handler; `src/api/execute.py` and `src/api/codegen.py` delegate to it; the CLI's
auto-generated groups call the daemon's command API). A single paused-command gate in
`execute()` therefore gives every surface an identical, honest answer:

```json
{"success": false, "error": "memory is paused (memory.enabled=false)"}
{"success": false, "error": "playbooks are paused (playbooks.enabled=false)"}
```

This also converts what would otherwise be a confusing `Unknown command: memory_search`
(the plugin that registers it isn't loaded) into an actionable message.

### 3.4 Announce at startup; report as info

Silence is the enemy of a pause. At startup, `Orchestrator.initialize()` logs one
unmissable line per paused subsystem stating *what* is off, *which flag* controls it, and
*that data is preserved*. `aq doctor` (owned by the trust-and-ops workstream, G.1) must
report paused subsystems as **info, never as an error or failed check** — a paused feature
is a configured state, not a fault. The check contract is specified in the implementation
spec §7; doctor just consumes it.

---

## 4. Memory Pause — Scope

### 4.1 What turns off

| Behavior | Mechanism |
|---|---|
| aq-memory plugin (Milvus, semantic search, KV, consolidation crons, its `memory_*` commands/tools/CLI/Discord groups) | not loaded — skipped by name in `PluginRegistry.load_all()` |
| L1 facts / L1 guidance / L2 topic context in task prompts | `get_service("memory")` returns `None` → existing skip path in `src/orchestrator/execution.py` |
| L1/L2 in the Supervisor chat prompt | same `get_service` skip in `Supervisor._build_system_prompt` |
| `memory_*` commands, MCP tools, `aq memory save\|search` | command gate (§3.3) returns the paused error |
| ReflectionEngine (post-task and in-chat reflection) | constructed with level forced to `"off"` when memory is paused — `should_reflect()` is then always false |
| Memory consolidation trigger | doubly inert: the `memory-consolidation` playbook's `timer.24h` trigger never fires (playbooks paused) and the plugin that would execute consolidation isn't loaded |
| Workspace spec watcher + reference-stub enricher | not constructed — they are memory-accumulation plumbing living under `config.memory.*`, so the section's master flag governs them |

### 4.2 What stays on

- **L0 role, project override, identity, task context, attachments, tools** — the entire
  prompt pipeline except the two memory tiers. `PromptBuilder` is unchanged: its
  `set_l1_*`/`set_l2_*` setters simply never receive text (empty strings are already
  skipped in `build()`), and the L1/L2 **slots remain** so `aq prime` can fill them at the
  comeback (delivery path owned by the aq-surface/Workstream C+E specs — referenced, not
  respecified here).
- **Vault memory files.** Everything under `vault/**/memory/`, `facts.md`, knowledge
  topics — untouched and still human-editable. The facts vault-watcher handlers stay
  registered in their existing service-less "log-only" mode.
- **Work-state contract and handoff** (todo §7) — these are task state, not memory; owned
  by the work-graph/session-runtime specs.
- **Semantic tool index** (`ToolRegistry.build_tool_index`) — reads embedding settings from
  `config.memory` but is tool routing, not a memory tier; it already degrades gracefully
  when `memsearch` is unavailable. Out of scope for the pause.
- **`process_task_completion`** (plan discovery on task completion) — not reflection; stays.

---

## 5. Playbooks Pause — Scope

### 5.1 What turns off

With `playbooks.enabled=false`, `Orchestrator.initialize()` skips the entire playbook
wiring block: `PlaybookManager` (and its compile-provider), `load_from_disk`, orphan
prune, the background compilation reconcile, trigger subscription, the playbook vault-
watcher handlers (so the watcher ignores `playbooks/` dirs by having no handler registered
for them), `TimerService`, `PlaybookResumeHandler`, `WorkflowStageResumeHandler`, and
`OrphanWorkflowRecovery` (including its startup recovery pass). Two housekeeping methods
whose callers survive get early returns: the paused-run timeout sweep and workflow-stage
completion checks. All `*_playbook*` and `*_workflow*` commands return the paused error.

A pleasant side effect: startup no longer spends LLM tokens or minutes on playbook
compilation reconcile.

### 5.2 TimerService: verified sole-consumer analysis

The canonical concern was that pausing playbooks might starve other `timer.*`/`cron.*`
consumers. Verified by reading the code: `TimerService` derives its **entire** timer/cron
map from `PlaybookManager.get_all_triggers()` — playbooks are its only producer of
schedules and its only consumer of the resulting events. Plugin cron is a **separate
mechanism** (`@cron` decorator in `src/plugins/base.py`, collected in
`PluginRegistry.load_plugin`, ticked by `PluginRegistry.tick_cron()` from the cascade's
step 7b) and keeps running. Therefore TimerService is simply not constructed while
playbooks are paused; nothing else loses its clock.

### 5.3 What stays on

- **Plugin cron** (above) — internal plugins' scheduled jobs keep firing.
- **The hardcoded cascade** — approvals, resume-paused, DEFINED promotion, stuck
  monitoring, auto-archive, log cleanup. Housekeeping that playbooks were meant to
  eventually absorb (gate sweep, reaper, archive) **stays as hardcoded cascade steps**,
  owned by the session-runtime / work-graph / worktree specs — we do not build an orders
  engine for them now (todo §8).
- **SYNC workflows** (`SyncWorkflowMixin`, `task_type=SYNC`) — workspace synchronization is
  core orchestration, unrelated to coordination-playbook "workflows"; unaffected.
- **Default playbook seeding** (`ensure_default_playbooks` in `src/vault.py`) — copies
  markdown into the vault only; harmless data, keeps vault structure stable; unchanged.
- **The vault watcher itself** — keeps watching profiles, facts, MCP servers, overrides.

### 5.4 Data preserved

`playbook_runs` rows (including runs paused mid-flight at pause time), compiled JSON under
`{data_dir}/compiled/`, and all playbook markdown in the vault are left exactly as they
are. Paused runs will simply never resume or time out while the flag is off; on re-enable
the existing timeout sweep and resume handlers pick them up unchanged.

---

## 6. Chat Analyzer and Dormant Modules

- **Chat analyzer `observe()`**: the real switch — verified in `src/chat_observer.py` and
  `src/config.py` — is `supervisor.observation.enabled` (there is no `chat_analyzer.enabled`;
  the `chat_analyzer:` section only tunes post-observe gates). Its default flips to
  `false`; the Discord bot already gates `ChatObserver` construction on it, so the observer
  is simply never built.
- **`src/runtimes/supervisor.py` + `src/chat_providers/` go dormant, not paused here.**
  They keep serving chat (Discord/CLI → `Supervisor.chat()`) until the new supervisor-agent
  lands; unwiring them from boot is **owned by the supervisor-agent spec** (todo §4, B.3)
  and is explicitly out of scope for this one. While memory is paused the Supervisor runs
  with empty L1/L2 tiers and reflection off — no code change needed beyond §4.
- **Rules/hook-engine**: already removed (playbooks spec §13 Phase 3 — migration complete).
  Only comments and one no-op stub (`PromptBuilder.load_relevant_rules`) remain; the stub's
  deletion is deferred to the aq-surface prompt work rather than done here, because callers
  in the frozen SDK runtime still reference it. Nothing further to remove in this spec.

---

## 7. Data Preservation Guarantees

- **Nothing is deleted** by flipping either flag — no DB rows, no vault files, no compiled
  artifacts, no Milvus collections.
- **Vault memory stays live for humans.** Files remain readable/editable in Obsidian; edits
  simply aren't indexed or synced until re-enable (the file is the source of truth —
  principle #1 — so re-enable re-reads files, never the reverse).
- **Milvus containers/processes may be stopped by the operator** while memory is paused —
  nothing in the daemon will touch `~/.agent-queue/memsearch/` or a Milvus server. Document
  in ops notes: stopping it saves resources; leaving it running is also fine; the data dir
  must not be deleted.
- **No schema migrations** are part of the pause, so `alembic downgrade` questions do not
  arise.

---

## 8. Code Freeze Policy

`src/playbooks/`, the workflow coordination modules (`workflow_stage_resume_handler.py`,
`orphan_workflow_recovery.py`, `workflow_pipeline_view.py`), `src/timer_service.py`,
`src/reflection.py`, and `src/chat_observer.py` are **frozen: bug fixes only**. The
aq-memory plugin repo is frozen on the same terms.

**Decision — tests keep running against the frozen code.** All existing unit tests (28
playbook test files and the memory/reflection suites) construct their subjects directly and
continue to pass; they are the regression net that makes "bug fixes only" safe and keeps
the re-enable path honest. Skip markers are reserved for tests that become *impossible*
under the pause (none identified), not for convenience. Integration-style tests that boot
the orchestrator and expect paused subsystems must opt in explicitly
(`memory.enabled=true` / `playbooks.enabled=true` in their fixture config) — relying on
defaults is now a bug in the test.

---

## 9. Un-Pause Criteria & Path

Re-enabling is deliberate, not casual. Before flipping either flag back (todo §11 Phase 4):

1. Workstreams A–C (sessions, worktrees, supervisor, `aq` surface) are stable in daily use.
2. For memory: the `aq prime` delivery path exists and the comeback plan (todo §7 — plugin
   re-enable, SQLite-FTS decision, `aq memory search` fragment, reflection as judgment
   task) has an approved spec.
3. For playbooks: the comeback design (orders/formulas/judgment tasks *or* deterministic
   compile — todo §8) has an approved spec. Playbooks likely return as that redesign rather
   than a bare flag flip — the flag still gates whatever returns.

The mechanical path is identical for both: set `enabled: true`, restart the daemon, watch
startup logs confirm the subsystem constructed, run its health command. **No migrations, no
backfills, no re-indexing steps are required by design**; if a bug fix during the freeze
would break that promise, it must ship with its own remediation and update this spec.

---

## 10. Interaction With Other Overhaul Specs

| Concern | Owner |
|---|---|
| `aq prime` assembly; empty-but-present L1/L2 slots | aq-surface spec (Workstream C/E) |
| Supervisor/chat-provider unwiring from boot | supervisor-agent spec (Workstream B) |
| Hardcoded housekeeping cascade steps (gate sweep, reaper) | session-runtime / work-graph / worktree specs |
| `aq doctor` implementation (consumes §3.4's check contract) | trust-and-ops (G.1) |
| Work-state contract, handoff | work-graph / session-runtime specs |

This spec owns: the flags, the disable points, the no-op behaviors, data preservation, the
freeze, and the un-pause path — detailed in
`docs/specs/implementation/feature-pauses.md`.
