---
tags: [implementation, feature-pauses, memory, playbooks, config, feature-flags]
---

# Feature Pauses — Implementation

**Status:** Draft — approved direction (2026-08-19)
**Related:** [[../design/feature-pauses]] (design of record — read first),
`docs/analysis/framework-overhaul-todo.md` (§7 E, §8 P), [[../design/self-improvement]],
[[../design/playbooks]], `docs/specs/config.md`, `docs/specs/command-handler.md`

All file/function references below were verified by reading the code on 2026-08-19.
Line numbers are approximate anchors, not contracts.

---

## 1. Summary

Two restart-required flags in `~/.agent-queue/config.yaml`, both defaulting to **false**:

```yaml
memory:
  enabled: false      # TEMPORARY (overhaul Phase 0): pauses aq-memory, L1/L2, reflection
playbooks:
  enabled: false      # TEMPORARY (overhaul Phase 0): pauses PlaybookManager & workflows
```

Paused components are **not constructed** at startup (design §3.2). A single gate in
`CommandHandler.execute()` makes every command surface (Discord, MCP, CLI, HTTP API)
return the canonical paused error. No data is deleted; re-enable is config flip + restart
with **no migrations by design**.

Canonical error payloads — exact strings, used everywhere:

```json
{"success": false, "error": "memory is paused (memory.enabled=false)"}
{"success": false, "error": "playbooks are paused (playbooks.enabled=false)"}
```

---

## 2. Config Changes (`src/config.py`)

1. **`MemoryConfig.enabled` default flip** — dataclass default `True → False` (~line 296)
   **and** the raw-parse default `mem.get("enabled", True) → False` (~line 1654). Both
   must change together (the parse default is authoritative for partial `memory:`
   sections). Update the docstring: "Paused by default during the framework overhaul —
   see docs/specs/design/feature-pauses.md."
2. **New `PlaybooksConfig`** dataclass:

   ```python
   @dataclass
   class PlaybooksConfig:
       """Playbook subsystem switch. Paused by default during the framework
       overhaul — see docs/specs/design/feature-pauses.md. Temporary."""
       enabled: bool = False

       def validate(self) -> list[ConfigError]:
           return []
   ```

   - `AppConfig.playbooks: PlaybooksConfig = field(default_factory=PlaybooksConfig)`
     (next to `memory`, ~line 843).
   - Raw parse: `if "playbooks" in raw:` block next to the existing
     `max_daily_playbook_tokens` parsing (~line 1478);
     `enabled=pb.get("enabled", False)`.
   - Existing `max_daily_playbook_tokens` / `max_concurrent_playbook_runs` stay top-level
     (freeze = minimal churn).
3. **`ObservationConfig.enabled` default flip** (`True → False`, ~line 480) and its parse
   default `observation.get("enabled", True) → False` (~line 1584). This is the real chat
   analyzer switch (verified: `src/chat_observer.py:111-129,245` reads
   `config.supervisor.observation.enabled`; `src/discord/bot.py:146-151` gates
   `ChatObserver` construction on it). There is no `chat_analyzer.enabled` — the
   `chat_analyzer:` section only tunes post-observe gates and is untouched.
4. **Hot-reload classification** — add `"playbooks"` to `RESTART_REQUIRED_SECTIONS`
   (~line 1105) and to `_SECTION_FIELDS` (~line 1120). `memory` is already
   restart-required. Do **not** add either to `HOT_RELOADABLE_SECTIONS` or
   `reload_non_critical()`.
5. **Config editor** — `src/config_editor.py` `get_config_schema()`: expose
   `playbooks.enabled` and mark both flags `restart_required: true` with a
   "temporary — overhaul pause" description.
6. **Docs** — `docs/specs/config.md`: document both flags as temporary with a pointer to
   the design spec.

---

## 3. Memory Disable Points

| # | File / function | Change |
|---|---|---|
| M1 | `src/orchestrator/core.py` → `Orchestrator.initialize()` plugin load (~line 1171) | Call `load_all()` with a skip set: `skip = frozenset({"aq-memory", "memory"}) if not self.config.memory.enabled else frozenset()`. Log the §6 startup line here. |
| M2 | `src/plugins/registry.py` → `PluginRegistry.load_all()` (~line 345) | New optional param `skip: frozenset[str] = frozenset()`. In the DB-row loop (~line 358): `if plugin_row["id"] in skip: logger.info("Plugin '%s' skipped (paused by config)", ...); continue`. **Do not** mutate the plugin's DB status — `disable_plugin()` is *not* used, so re-enable needs no DB change. Registry stays policy-free; the orchestrator owns the decision. |
| M3 | `src/orchestrator/execution.py` → `ExecutionMixin._execute_task()` (~lines 500–536) | **No change.** `plugin_registry.get_service("memory")` returns `None` when the plugin isn't loaded; the existing `if mem_svc:` guard skips L1 facts, L1 guidance, and L2 topic context. `Task.l1_facts/l1_guidance/l2_context` stay `""`. |
| M4 | `src/runtimes/claude_sdk.py` (~lines 1144–1152) and `src/prompt_builder.py` setters | **No change.** Empty strings are skipped by the existing setters/`build()`; the L1/L2 slots remain for the `aq prime` comeback (owned by aq-surface). |
| M5 | `src/runtimes/supervisor.py` → `Supervisor.__init__` (~line 226) | Force reflection off: `refl_cfg = config.supervisor.reflection; if not config.memory.enabled: refl_cfg = dataclasses.replace(refl_cfg, level="off")`. `ReflectionEngine.should_reflect()`/`determine_depth()` (verified `src/reflection.py:122,147`) then always decline — covering all three call sites (`_chat_inner` ×2, `_on_task_completed_unlocked` → `reflect()` at ~1762, reached from `src/orchestrator/approval.py:238`). |
| M6 | `src/runtimes/supervisor.py` → `_build_system_prompt()` (~lines 508–545) | **No change.** Same `get_service("memory")` guard as M3. |
| M7 | `src/orchestrator/core.py` → facts re-wiring (~line 1193) | **No change.** `mem_svc` is `None`, so `register_facts_handlers(..., service=mem_svc)` re-wiring is skipped; the earlier service-less registration (~line 912) stays — facts files remain watched in log-only mode. |
| M8 | `src/orchestrator/core.py` → `WorkspaceSpecWatcher` (~line 1107) and `ReferenceStubEnricher` (~line 1131) | Add `self.config.memory.enabled and` to both existing conditions. These are memory-accumulation plumbing under `config.memory.*`; the section master flag governs them. |
| M9 | `src/commands/handler.py` → `CommandHandler.execute()` | Memory command gate — see §5. |
| M10 | Consolidation trigger | **No change needed.** Doubly inert: the plugin's `@cron` jobs are never collected (plugin not loaded) and the `memory-consolidation` playbook's `timer.24h` never fires (playbooks paused). |

Unchanged by intent (record in code comments where touched): semantic tool index
(`ToolRegistry.build_tool_index`, degrades gracefully without memsearch),
`process_task_completion` plan discovery, vault memory files and their human editability.

---

## 4. Playbooks Disable Points

All in `src/orchestrator/core.py` → `Orchestrator.initialize()` unless noted. Wrap the
contiguous playbook wiring block (~lines 947–1078) in
`if self.config.playbooks.enabled:` with an `else:` that sets the attributes to `None`
and logs the §6 line. The skipped pieces, in order:

| # | Component (verified location) | Paused behavior |
|---|---|---|
| P1 | Playbook compile chat provider fallback (~line 950) | Not created — no LLM provider spun up for compilation. |
| P2 | `PlaybookManager` construction, `load_from_disk()`, `prune_orphan_compilations()`, background `reconcile_compilations()` (~lines 962–998) | `self.playbook_manager = None`. Compiled JSON under `{data_dir}/compiled/` untouched (no prune, no recompile). Startup no longer spends tokens on reconcile. |
| P3 | `on_trigger` wiring + `subscribe_to_events()` (~lines 1003–1005) | No event subscriptions; `_on_playbook_trigger` (~line 535) stays defined but unreachable. |
| P4 | `register_playbook_handlers(self.vault_watcher, ...)` (~line 1007; `src/playbooks/handler.py:229`) | Not called — the vault watcher has no handler for `PLAYBOOK_PATTERNS`, i.e. it *ignores* `playbooks/` dirs while continuing to watch profiles/facts/mcp-servers/overrides. |
| P5 | `TimerService` construction + `start()` (~lines 1018–1023) | `self.timer_service = None` (already the `__init__` default, ~line 313). Verified sole-consumer: its timer map comes exclusively from `PlaybookManager.get_all_triggers()` (`src/timer_service.py:317,364`) and nothing else in `src/` consumes `timer.*`/`cron.*` events. Plugin cron is separate and stays ON (`@cron` in `src/plugins/base.py` → `PluginRegistry.tick_cron()` at `run_one_cycle` step 7b, ~line 1781). |
| P6 | `PlaybookResumeHandler` + `.subscribe()` (~lines 1033–1040) | Not created — `human.review.completed` no longer resumes runs. |
| P7 | `WorkflowStageResumeHandler` + `.subscribe()` (~lines 1049–1056) | Not created. |
| P8 | `OrphanWorkflowRecovery` + `recover_on_startup()` (~lines 1064–1078) | Not created; startup recovery pass skipped; the periodic check at `run_one_cycle` 7e (~line 1807) is already `None`-guarded. |
| P9 | `src/orchestrator/monitoring.py` → `_check_paused_playbook_timeouts()` (line 335) | Early return: `if not self.config.playbooks.enabled: return` before the handler delegation (~line 347). Caller at `run_one_cycle` step 12 (~line 1837) stays. Paused `playbook_runs` rows are left as-is — they neither resume nor time out. |
| P10 | `src/orchestrator/events.py` → `_check_workflow_stage_completion()` (line 212) | Early return on the flag. One edit covers all four call sites (`approval.py:196`, `core.py:673`, `execution.py:1272`, `monitoring.py:161`). |
| P11 | `Orchestrator.shutdown()` (~lines 1649–1654) | **No change** — existing `if self.timer_service:` / `hasattr` guards already tolerate `None`/absent attributes. Set `self.playbook_manager = None`, `self.playbook_resume_handler = None`, etc. in the `else:` branch anyway so `getattr` sites (e.g. `src/commands/playbook_commands.py:44`) see `None`. |
| P12 | `src/commands/handler.py` → command gate | See §5. |

Explicitly **unchanged**: `SyncWorkflowMixin` (`task_type=SYNC` workspace sync — core
orchestration, not coordination workflows), `ensure_default_playbooks` in `src/vault.py`
(seeds markdown only), the hardcoded cascade steps (approvals/resume/promotion/stuck/
archive/log-cleanup), and the entire `src/playbooks/` package contents (frozen).

---

## 5. Command-Surface Gate (`src/commands/handler.py`)

In `CommandHandler.execute()` (~line 383), after project-id normalization and **before**
the `_cmd_{name}` lookup:

```python
paused_error = self._paused_command_error(name)
if paused_error:
    return {"success": False, "error": paused_error}
```

Module-level definitions:

```python
PAUSED_PLAYBOOK_COMMANDS: frozenset[str] = frozenset({
    # src/commands/playbook_commands.py (16)
    "list_playbooks", "list_playbook_runs", "inspect_playbook_run", "resume_playbook",
    "recover_workflow", "compile_playbook", "show_playbook_graph", "run_playbook",
    "dry_run_playbook", "playbook_health", "playbook_graph_view", "get_playbook_source",
    "update_playbook_source", "set_playbook_enabled", "create_playbook", "delete_playbook",
    # src/commands/workflow_commands.py (5)
    "create_workflow", "get_workflow", "list_workflows", "advance_workflow_stage",
    "workflow_pipeline_view",
})

def _is_memory_command(name: str) -> bool:
    return name == "memory" or name.startswith(("memory_", "memory.")) \
        or name in {"compact_memory"}
```

Rules: playbook/workflow names gate on `not self.config.playbooks.enabled` (read-only
commands included — one crisp contract); memory names gate on
`not self.config.memory.enabled`. Memory command names are owned by the external
aq-memory plugin, so the prefix rule is primary and the explicit extras set is updated
from the plugin's registered names at implementation time (`memory_*` per
`src/cli/auto_commands.py:9`, plus `compact_memory`, ~line 121).

Coverage, verified: Discord slash commands, embedded MCP tools (`src/embedded_mcp.py:73`
shares the handler), HTTP API (`src/api/execute.py:35`, `src/api/codegen.py:119`), and
`aq` CLI auto-groups all dispatch through `execute()`. `aq memory save|search` therefore
performs no action and prints the exact memory message (exit code follows the CLI's normal
error convention). Dashboard playbook pages render the paused error — accepted.

---

## 6. Startup Logging & Doctor Check

Exact startup lines (INFO, once each, from `Orchestrator.initialize()`):

```
Memory subsystem PAUSED (memory.enabled=false) — aq-memory plugin not loaded; L1/L2 prompt tiers empty; reflection off; data preserved. See docs/specs/design/feature-pauses.md
Playbooks subsystem PAUSED (playbooks.enabled=false) — PlaybookManager/TimerService/resume handlers/workflow recovery not started; playbook_runs and compiled JSON preserved. See docs/specs/design/feature-pauses.md
```

**Doctor contract** (implementation owned by trust-and-ops, G.1): for each flag that is
false, emit `{"check": "memory"|"playbooks", "ok": true, "state": "paused",
"severity": "info", "flag": "memory.enabled"|"playbooks.enabled"}`. Paused is a configured
state — it must never fail a check, never appear as a warning, and must name the flag.
Optionally mirror the same shape into `_health_checks()` in `src/main.py` (nice-to-have).

---

## 7. Data Preservation

- No deletions anywhere: `playbook_runs`, `workflows` tables, compiled JSON, vault
  markdown, Milvus data — all untouched. M2 explicitly avoids `disable_plugin()` so the
  plugin's DB row keeps its status.
- Vault memory/playbook files stay human-editable; edits take effect on re-enable
  (recompile / re-index happen through the existing change-detection paths, not a special
  migration).
- **Operator note (document in ops/README):** Milvus (or the `~/.agent-queue/memsearch/`
  milvus-lite file) may be stopped while paused; the daemon will not touch it. Do not
  delete its data directory.
- No Alembic migration is part of this change (`src/database/tables.py` untouched).

---

## 8. Freeze Policy & Tests

Frozen (bug fixes only): `src/playbooks/`, `src/timer_service.py`,
`src/workflow_stage_resume_handler.py`, `src/orphan_workflow_recovery.py`,
`src/workflow_pipeline_view.py`, `src/reflection.py`, `src/chat_observer.py`,
`src/commands/playbook_commands.py`, `src/commands/workflow_commands.py`, and the
aq-memory plugin repo.

**Decision: tests keep running.** The 28 `tests/test_playbook*` files and the
memory/reflection suites construct their subjects directly and must keep passing — they
guard the frozen code and the re-enable path. No blanket skip markers; a skip is allowed
only for a test the pause makes impossible, with `@pytest.mark.skip(reason="paused: ...")`
naming this spec. Tests that boot the orchestrator/config and need paused subsystems
alive must set `memory.enabled=True` / `playbooks.enabled=True` /
`supervisor.observation.enabled=True` explicitly in their fixtures — a sweep for tests
relying on the old defaults is part of the checklist.

---

## 9. Un-Pause Path

1. Preconditions per design spec §9 (Phase 4 criteria; approved comeback spec).
2. Edit `~/.agent-queue/config.yaml`: set the flag(s) to `true`.
3. Restart the daemon (both flags are restart-required; hot-reload will not apply them).
4. Verify: startup PAUSED lines absent; "Loaded plugin: aq-memory" / "Subscribed to N
   playbook trigger event(s)" present; `memory_search` / `list_playbooks` return real
   results; doctor drops the info entries.
5. **No migrations, no backfills, no re-indexing required — by design.** Any freeze-window
   bug fix that would violate this must ship its own remediation and amend this section.

---

## 10. Test Plan

- **Config:** defaults — fresh `AppConfig()` has `memory.enabled is False`,
  `playbooks.enabled is False`, `supervisor.observation.enabled is False`; partial YAML
  sections keep those defaults; explicit `enabled: true` round-trips; `playbooks` appears
  in `RESTART_REQUIRED_SECTIONS` and `diff_configs` output.
- **Gate:** for every name in `PAUSED_PLAYBOOK_COMMANDS` and representative memory names
  (`memory_search`, `memory_save`, `compact_memory`), `execute()` returns the exact
  canonical payload when paused, and dispatches normally when enabled. Unknown non-paused
  commands still return `Unknown command: ...`.
- **Orchestrator startup (paused):** `initialize()` leaves `playbook_manager`,
  `timer_service`, `playbook_resume_handler`, `workflow_stage_resume_handler`,
  `orphan_workflow_recovery`, `workspace_spec_watcher`, `reference_stub_enricher` as
  `None`; no `timer.*` events across N cycles; `plugin_registry.tick_cron()` still runs;
  vault watcher has no `playbook:` handler ids but keeps facts/profile handlers; both
  PAUSED log lines emitted; `run_one_cycle()` completes cleanly ×10; `shutdown()` clean.
- **Startup (enabled):** flags true ⇒ wiring identical to pre-change behavior (guarded by
  the existing integration tests once fixtures opt in).
- **Memory skip:** `load_all(skip={"aq-memory"})` skips without DB status change;
  `get_service("memory") is None`; a task executed end-to-end (fake runtime) gets
  `l1_facts == l1_guidance == l2_context == ""` while L0 role and task context are intact.
- **Reflection:** Supervisor built with `memory.enabled=False` has
  `reflection.level == "off"`; `should_reflect("task.completed")` is False;
  `on_task_completed` still returns `process_task_completion`'s result.
- **Housekeeping early-returns:** `_check_paused_playbook_timeouts` and
  `_check_workflow_stage_completion` return immediately when paused and execute when not.
- **Data preservation:** seed `playbook_runs` + compiled JSON, boot paused, run cycles,
  shut down — bytes/rows identical; then boot enabled — runs visible to
  `list_playbook_runs`.
- **Sweep:** full `pytest tests/ -n auto` green after fixture opt-ins.

---

## 11. Risks & Mitigations

- **Default flip breaks tests silently relying on `enabled=True`.** Mitigation: checklist
  sweep + explicit fixture opt-ins (§8); CI run before merge.
- **Memory command names drift** (plugin owns them). Mitigation: prefix rule is primary;
  extras set reviewed against the plugin's registered names at implementation time.
- **Hidden `timer.*` consumer added later** would starve with TimerService off.
  Mitigation: verified none today; comment at the P5 skip site directs future consumers to
  plugin cron or a hardcoded cascade step.
- **In-flight `playbook_runs` frozen mid-pause** may reference playbook versions whose
  markdown changes during the freeze. Accepted: runs resume against recompiled versions on
  re-enable, same as today's restart semantics.
- **Operator confusion** ("why is memory_search failing?"). Mitigation: canonical error
  names the flag; startup lines; doctor info entries.
- **A freeze-window bug fix requiring a migration** would break the no-migration promise.
  Mitigation: policy in §9.5 — ship remediation + amend spec.

---

## 12. Checklist

- [ ] `src/config.py`: flip `MemoryConfig.enabled` default + parse default (§2.1)
- [ ] `src/config.py`: add `PlaybooksConfig`, `AppConfig.playbooks`, raw parse (§2.2)
- [ ] `src/config.py`: flip `ObservationConfig.enabled` default + parse default (§2.3)
- [ ] `src/config.py`: `playbooks` → `RESTART_REQUIRED_SECTIONS` + `_SECTION_FIELDS` (§2.4)
- [ ] `src/config_editor.py`: schema entries for both flags (§2.5)
- [ ] `docs/specs/config.md`: document both flags as temporary (§2.6)
- [ ] `src/plugins/registry.py`: `load_all(skip=...)` param (M2)
- [ ] `src/orchestrator/core.py`: pass memory skip set + memory PAUSED log line (M1)
- [ ] `src/orchestrator/core.py`: gate spec watcher + stub enricher on `memory.enabled` (M8)
- [ ] `src/runtimes/supervisor.py`: force reflection level `"off"` when memory paused (M5)
- [ ] `src/orchestrator/core.py`: wrap playbook wiring block; `None` attributes; playbooks
      PAUSED log line (P1–P8, P11)
- [ ] `src/orchestrator/monitoring.py`: early return in `_check_paused_playbook_timeouts` (P9)
- [ ] `src/orchestrator/events.py`: early return in `_check_workflow_stage_completion` (P10)
- [ ] `src/commands/handler.py`: `PAUSED_PLAYBOOK_COMMANDS`, memory-name matcher, gate in
      `execute()` with exact error strings (§5)
- [ ] Verify memory command extras against the aq-memory plugin's registered names (§5)
- [ ] Sweep tests for implicit reliance on old defaults; add fixture opt-ins (§8)
- [ ] New tests per §10 (config defaults, gate, paused startup, reflection off,
      early-returns, data preservation)
- [ ] Hand doctor check contract (§6) to trust-and-ops; optional `_health_checks()` mirror
- [ ] Ops note: Milvus may be stopped while paused; never delete its data dir (§7)
- [ ] Annotate `docs/specs/design/self-improvement.md` and `playbooks.md` Status lines as
      "Paused (feature-pauses.md)" — no content rewrite
- [ ] `pytest tests/ -n auto` green; manual smoke: boot paused daemon, run one task, confirm
      empty L1/L2 and no `timer.*` events
