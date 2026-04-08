---
tags: [design, roadmap, planning]
---

# Implementation Roadmap

**Related:** [[guiding-design-principles]], [[playbooks]], [[vault]], [[memory-plugin]],
[[memory-scoping]], [[profiles]], [[self-improvement]], [[agent-coordination]]

This roadmap breaks the design specs into phased work with explicit dependencies
and testing checkpoints. Phases are ordered so each builds on the last. Within
each phase, tasks are grouped into workstreams that can run in parallel where
dependencies allow.

---

## Phase 0: Prerequisite Refactors

These changes prepare the existing codebase for the new systems. They are
independently valuable and can ship without any design spec work landing.

**Source:** [[playbooks]] Section 17

### 0.1 EventBus Payload Filtering

Extend EventBus subscriptions to support dict-based payload filters.

| # | Task | Depends On |
|---|---|---|
| 0.1.1 | Add `filter` parameter to `EventBus.subscribe()` | — |
| 0.1.2 | Implement filter matching logic (all conditions must match) | 0.1.1 |
| 0.1.3 | Add tests: filtered subscription receives only matching events | 0.1.2 |
| 0.1.4 | Add tests: unfiltered subscriptions continue to work (backward compat) | 0.1.2 |
| 0.1.5 | Add tests: events missing filter fields are skipped | 0.1.2 |

> **Test checkpoint:** Run full existing event-bus test suite + new filter tests.
> Verify zero regressions in hook engine and orchestrator event handling.

### 0.2 Event Schema Registry

Lightweight validation for event payloads.

| # | Task | Depends On |
|---|---|---|
| 0.2.1 | Define `EVENT_SCHEMAS` dict with required/optional fields per event type | — |
| 0.2.2 | Implement `validate_event(event_type, payload)` function | 0.2.1 |
| 0.2.3 | Wire validation into `EventBus.emit()` (warn in prod, error in dev) | 0.2.2 |
| 0.2.4 | Add schemas for all existing event types (task.*, note.*, file.*, plugin.*, config.*) | 0.2.1 |
| 0.2.5 | Add tests: valid events pass, missing required fields warn/error | 0.2.3 |

### 0.3 GitManager Event Emission

Add event emission to existing git operations.

| # | Task | Depends On |
|---|---|---|
| 0.3.1 | Define schemas for `git.commit`, `git.push`, `git.pr.created` events | 0.2.1 |
| 0.3.2 | Emit `git.commit` from `GitManager.acommit_all()` with commit hash, branch, changed files, project_id, agent_id | — |
| 0.3.3 | Emit `git.push` from `GitManager.apush_branch()` | — |
| 0.3.4 | Emit `git.pr.created` from `GitManager.create_pr()` | — |
| 0.3.5 | Add tests: verify events emitted with correct payloads | 0.3.2, 0.3.3, 0.3.4 |

> **Test checkpoint:** Create a task, let an agent commit + push + create PR.
> Verify all three git events fire with correct payloads in the event log.

### 0.4 Supervisor Configuration Flexibility

Enable per-call model and tool overrides.

| # | Task | Depends On |
|---|---|---|
| 0.4.1 | Add `llm_config` parameter to `Supervisor.chat()` | — |
| 0.4.2 | Implement chat provider swap based on `llm_config` within a call | 0.4.1 |
| 0.4.3 | Add `tool_overrides` parameter to `Supervisor.chat()` for restricting tool set | 0.4.1 |
| 0.4.4 | Add tests: verify model override produces response from correct provider | 0.4.2 |
| 0.4.5 | Add tests: verify tool restriction prevents unauthorized tool calls | 0.4.3 |

### 0.5 Task Records Migration

Move task records out of the memory search path.

| # | Task | Depends On |
|---|---|---|
| 0.5.1 | Create `~/.agent-queue/tasks/{project_id}/` directory structure | — |
| 0.5.2 | Update task record write path to use new location | 0.5.1 |
| 0.5.3 | Write migration script to move existing `memory/*/tasks/` to `tasks/*/` | 0.5.1 |
| 0.5.4 | Update memory indexer to exclude the old tasks path | 0.5.2 |
| 0.5.5 | Re-index existing project memory collections (without task files) | 0.5.4 |
| 0.5.6 | Add tests: verify task records write to new path, memory search no longer returns task files | 0.5.5 |

> **Test checkpoint:** Full system test — create tasks, complete them, verify records
> appear in new location. Memory search should return cleaner results without task noise.

---

## Phase 1: Vault Structure

Create the vault directory layout and the unified file watcher. This is the
foundation everything else builds on.

**Source:** [[vault]]

### 1.1 Vault Directory Creation

| # | Task | Depends On |
|---|---|---|
| 1.1.1 | Create vault directory structure at `~/.agent-queue/vault/` with all subdirectories (system/, orchestrator/, agent-types/, projects/, templates/) | — |
| 1.1.2 | Add vault path constants to `AppConfig` | — |
| 1.1.3 | Create `vault_manager.py` module for vault path resolution and directory creation | 1.1.1, 1.1.2 |
| 1.1.4 | Wire vault initialization into orchestrator startup | 1.1.3 |

### 1.2 Content Migration

| # | Task | Depends On |
|---|---|---|
| 1.2.1 | Move existing `.obsidian/` config from `memory/` to `vault/` | 1.1.1 |
| 1.2.2 | Move existing rule files from `memory/*/rules/` to `vault/system/playbooks/` (or project playbooks) | 1.1.1 |
| 1.2.3 | Move existing notes from `notes/` to `vault/projects/*/notes/` | 1.1.1 |
| 1.2.4 | Copy existing project memory files to `vault/projects/*/memory/` | 1.1.1 |
| 1.2.5 | Write migration script that handles all moves idempotently | 1.2.1–1.2.4 |
| 1.2.6 | Add startup check: if old paths exist and vault is empty, run migration | 1.2.5 |

### 1.3 Unified Vault File Watcher

| # | Task | Depends On |
|---|---|---|
| 1.3.1 | Implement `VaultWatcher` class using existing `FileWatcher` pattern | 1.1.1 |
| 1.3.2 | Implement path-based dispatch: `*/playbooks/*.md` → playbook handler | 1.3.1 |
| 1.3.3 | Implement path-based dispatch: `*/profile.md` → profile handler | 1.3.1 |
| 1.3.4 | Implement path-based dispatch: `*/memory/**/*.md` → memory re-index handler | 1.3.1 |
| 1.3.5 | Implement path-based dispatch: `projects/*/README.md` → orchestrator summary handler | 1.3.1 |
| 1.3.6 | Implement path-based dispatch: `*/overrides/*.md` → override re-index handler | 1.3.1 |
| 1.3.7 | Implement path-based dispatch: `*/facts.md` → KV sync handler | 1.3.1 |
| 1.3.8 | Wire `VaultWatcher` into orchestrator startup and tick loop | 1.3.1 |
| 1.3.9 | Add tests: file changes in each path trigger correct handler | 1.3.2–1.3.7 |

> **Test checkpoint:** Create vault structure, edit files in each location, verify
> the correct handler fires. Edit a profile.md, verify change is detected. Edit
> a memory file, verify re-index triggered. This is the foundation — it must be solid.

---

## Phase 2: memsearch Fork & Memory Plugin v2

Fork memsearch, add KV + temporal + topic support, build the new plugin.

**Source:** [[memory-plugin]]

### 2.1 memsearch Fork

| # | Task | Depends On |
|---|---|---|
| 2.1.1 | Fork [zilliztech/memsearch](https://github.com/zilliztech/memsearch) to internal repo | — |
| 2.1.2 | Add unified collection schema (entry_type, content, original, kv fields, valid_from/to, topic, tags) | 2.1.1 |
| 2.1.3 | Implement KV insert/query methods (scalar-only, no vector search) | 2.1.2 |
| 2.1.4 | Implement temporal insert/query methods (valid_from/valid_to windowed lookups) | 2.1.2 |
| 2.1.5 | Implement temporal fact lifecycle (close old window, open new on update) | 2.1.4 |
| 2.1.6 | Implement historical "as-of" query method | 2.1.4 |
| 2.1.7 | Implement topic field support (scalar filter before vector search) | 2.1.2 |
| 2.1.8 | Implement multi-collection parallel search with weighted merging | 2.1.2 |
| 2.1.9 | Implement scope-aware collection naming (`aq_system`, `aq_agenttype_*`, `aq_project_*`) | 2.1.2 |
| 2.1.10 | Implement tag-based cross-collection search | 2.1.2 |
| 2.1.11 | Implement `original` field storage (full content alongside summary embedding) | 2.1.2 |
| 2.1.12 | Implement retrieval tracking (update `retrieval_count`, `last_retrieved` on search results) | 2.1.2 |
| 2.1.13 | Add tests: KV insert/query round-trip | 2.1.3 |
| 2.1.14 | Add tests: temporal insert, update (close/open window), as-of query | 2.1.5, 2.1.6 |
| 2.1.15 | Add tests: topic-filtered search vs. unfiltered, fallback on < 3 results | 2.1.7 |
| 2.1.16 | Add tests: multi-collection weighted merge produces correct ranking | 2.1.8 |
| 2.1.17 | Add tests: cross-collection tag search | 2.1.10 |

> **Test checkpoint:** Run the full memsearch fork test suite against Milvus Lite.
> Verify all new features work in isolation before integrating with the plugin.

### 2.2 Memory Plugin v2 Skeleton

| # | Task | Depends On |
|---|---|---|
| 2.2.1 | Create `src/plugins/internal/memory_v2.py` plugin skeleton implementing InternalPlugin | — |
| 2.2.2 | Register plugin with PluginRegistry, coexisting with v1 during transition | 2.2.1 |
| 2.2.3 | Implement `MemoryService` v2 protocol wrapping the memsearch fork | 2.1.9, 2.2.1 |
| 2.2.4 | Implement MCP tool: `memory_search` (semantic search with optional topic filter) | 2.2.3 |
| 2.2.5 | Implement MCP tool: `memory_save` (with dedup, summary/original, topic auto-detect) | 2.2.3 |
| 2.2.6 | Implement MCP tool: `memory_list` (browse memories in a scope) | 2.2.3 |
| 2.2.7 | Implement MCP tool: `memory_recall` (KV lookup with scope resolution) | 2.2.3 |
| 2.2.8 | Implement MCP tool: `memory_store` (KV write to scope + vault fact file) | 2.2.3 |
| 2.2.9 | Implement MCP tool: `memory_list_facts` (list KV entries by scope/namespace) | 2.2.3 |
| 2.2.10 | Implement MCP tool: `memory_get` (unified auto-routing: KV first, then semantic) | 2.2.7, 2.2.4 |
| 2.2.11 | Implement facts.md parser (key:value pairs under markdown headings → KV entries) | 2.2.3 |
| 2.2.12 | Implement facts.md writer (KV changes → update vault fact file) | 2.2.11 |
| 2.2.13 | Wire facts.md file watcher handler from Phase 1.3 to facts.md parser | 1.3.7, 2.2.11 |
| 2.2.14 | Add tests: each MCP tool round-trip (save/search, store/recall, list) | 2.2.4–2.2.10 |
| 2.2.15 | Add tests: facts.md parse → KV insert → recall returns correct value | 2.2.11, 2.2.7 |
| 2.2.16 | Add tests: memory_get routes to KV for exact matches, semantic for fuzzy | 2.2.10 |

> **Test checkpoint:** End-to-end: an agent saves an insight via MCP, a second agent
> searches and finds it. An agent stores a fact, another agent recalls it. Both the
> vault markdown files and the Milvus collections are consistent.

---

## Phase 3: Memory Scoping & Tiers

Build the scope hierarchy, tiered loading, and override model.

**Source:** [[memory-scoping]]

### 3.1 Scope Resolution

| # | Task | Depends On |
|---|---|---|
| 3.1.1 | Implement scope resolver: given (agent_type, project_id), return ordered collection list with weights | 2.1.9 |
| 3.1.2 | Create per-agent-type collections on first profile creation | 3.1.1 |
| 3.1.3 | Create system-level collection on startup | 3.1.1 |
| 3.1.4 | Create orchestrator collection on startup | 3.1.1 |
| 3.1.5 | Migrate existing per-project collections to new naming convention | 3.1.1 |
| 3.1.6 | Implement first-match-wins KV scope resolution (project → agent-type → system) | 3.1.1 |
| 3.1.7 | Implement weighted merge for semantic search across scopes | 3.1.1 |
| 3.1.8 | Add tests: scope resolution returns correct collections in correct order | 3.1.1 |
| 3.1.9 | Add tests: KV lookup finds project-level fact, falls through to system when not found | 3.1.6 |
| 3.1.10 | Add tests: semantic search merges results from multiple scopes with correct weighting | 3.1.7 |

### 3.2 Override Model

| # | Task | Depends On |
|---|---|---|
| 3.2.1 | Implement override file indexing (`vault/projects/{id}/overrides/{type}.md` into project collection) | 3.1.1 |
| 3.2.2 | Wire override file watcher handler from Phase 1.3 | 1.3.6, 3.2.1 |
| 3.2.3 | Implement override injection into agent context alongside base profile | 3.2.1 |
| 3.2.4 | Add tests: override content appears in search results with highest weight | 3.2.1 |
| 3.2.5 | Add tests: override for agent-type X doesn't appear in agent-type Y searches | 3.2.1 |

### 3.3 Memory Tiers

| # | Task | Depends On |
|---|---|---|
| 3.3.1 | Implement L0 injection: extract `## Role` from profile.md into agent system prompt | 2.2.3 |
| 3.3.2 | Implement L1 injection: eager-load project + agent-type facts.md KV entries at task start | 2.2.7, 3.1.6 |
| 3.3.3 | Implement L2 topic detection from task description/context | 2.1.7 |
| 3.3.4 | Implement L2 topic-filtered memory loading when topic is detected | 3.3.3 |
| 3.3.5 | Wire L0 + L1 into task execution path (adapter context building) | 3.3.1, 3.3.2 |
| 3.3.6 | Wire L2 into task execution path (on-demand when topic emerges) | 3.3.4 |
| 3.3.7 | Add tests: L0+L1 present in every task context | 3.3.5 |
| 3.3.8 | Add tests: L2 topic memories appear when topic is relevant, absent when not | 3.3.6 |
| 3.3.9 | Add tests: L3 search still works and returns cross-topic results | 3.3.6 |

### 3.4 Deduplication & Summary

| # | Task | Depends On |
|---|---|---|
| 3.4.1 | Implement similarity-based dedup in `memory_save` (>0.95 timestamp update, 0.8-0.95 LLM merge, <0.8 create new) | 2.2.5 |
| 3.4.2 | Implement summary generation for long memories (>200 tokens → summarize for embedding, keep original) | 2.1.11, 2.2.5 |
| 3.4.3 | Add `full=true` parameter to `memory_get` to return original instead of summary | 2.2.10 |
| 3.4.4 | Add tests: duplicate save is deduplicated, similar save is merged, distinct save creates new | 3.4.1 |
| 3.4.5 | Add tests: long content is summarized for search but original is retrievable | 3.4.2, 3.4.3 |

> **Test checkpoint:** Full integration test: create an agent with a profile, set up
> project facts, create overrides. Start a task — verify L0 (role) and L1 (facts)
> are in the context. Save several insights with topics. Start another task on the
> same topic — verify L2 topic memories appear. Search across topics — verify L3
> returns cross-topic results with correct scope weighting.

---

## Phase 4: Profiles as Markdown

Move profiles from DB-only to markdown source of truth.

**Source:** [[profiles]]

### 4.1 Profile Parser & Sync

| # | Task | Depends On |
|---|---|---|
| 4.1.1 | Implement markdown profile parser: extract JSON blocks from `## Config`, `## Tools`, `## MCP Servers` | — |
| 4.1.2 | Implement English section extractor for `## Role`, `## Rules`, `## Reflection` | 4.1.1 |
| 4.1.3 | Implement JSON validation for each block (config schema, tool names vs registry, MCP structure) | 4.1.1 |
| 4.1.4 | Implement profile → DB sync (parsed fields → `agent_profiles` table upsert) | 4.1.1, 4.1.2 |
| 4.1.5 | Wire profile.md file watcher handler from Phase 1.3 to parser + sync | 1.3.3, 4.1.4 |
| 4.1.6 | Implement error handling: bad JSON → sync fails, previous config retained, notification sent | 4.1.4 |
| 4.1.7 | Update chat/dashboard profile commands to write to markdown file instead of DB | 4.1.4 |
| 4.1.8 | Add tests: valid profile.md parses correctly and syncs all fields to DB | 4.1.4 |
| 4.1.9 | Add tests: invalid JSON in profile.md triggers failure notification, DB unchanged | 4.1.6 |
| 4.1.10 | Add tests: edit profile.md in vault, verify DB updates within file watcher cycle | 4.1.5 |

### 4.2 Profile Migration

| # | Task | Depends On |
|---|---|---|
| 4.2.1 | Write migration script: read existing DB profiles → generate markdown files in vault | 4.1.1 |
| 4.2.2 | Create default profile templates in `vault/templates/` | — |
| 4.2.3 | Create orchestrator profile.md | 4.2.2 |
| 4.2.4 | Add startup check: if DB profiles exist but no vault markdown, run migration | 4.2.1 |

### 4.3 Starter Knowledge Packs

| # | Task | Depends On |
|---|---|---|
| 4.3.1 | Create starter knowledge files for `coding` agent type (common pitfalls, git conventions) | — |
| 4.3.2 | Create starter knowledge files for `code-review` agent type (review checklist) | — |
| 4.3.3 | Create starter knowledge files for `qa` agent type (testing patterns) | — |
| 4.3.4 | Implement knowledge pack copy on first profile.md creation (detect new profile, copy matching templates) | 4.1.5, 4.3.1 |
| 4.3.5 | Add tests: new agent type profile triggers knowledge pack copy, files tagged `#starter` | 4.3.4 |

> **Test checkpoint:** Create a new agent profile via chat command. Verify: markdown
> file appears in vault, DB row syncs, starter knowledge pack copied. Edit the
> profile.md in Obsidian — verify DB updates. Intentionally break JSON in profile —
> verify graceful failure.

---

## Phase 5: Playbook System

The core new automation system.

**Source:** [[playbooks]]

### 5.1 Playbook Compilation

| # | Task | Depends On |
|---|---|---|
| 5.1.1 | Define playbook JSON schema as a Python dataclass or JSON Schema file | — |
| 5.1.2 | Implement `PlaybookCompiler` class: reads markdown, invokes LLM, validates output against schema | 5.1.1 |
| 5.1.3 | Implement schema validation: entry node exists, no unreachable nodes, transitions reference valid nodes | 5.1.1 |
| 5.1.4 | Implement compiled JSON storage in `~/.agent-queue/compiled/` with scope-mirrored directory structure | 5.1.2 |
| 5.1.5 | Implement source_hash change detection (skip recompilation when unchanged) | 5.1.4 |
| 5.1.6 | Wire playbook file watcher handler from Phase 1.3 to compiler | 1.3.2, 5.1.2 |
| 5.1.7 | Implement compilation error handling (keep previous version, surface error notification) | 5.1.2 |
| 5.1.8 | Add tests: sample markdown compiles to valid JSON matching schema | 5.1.2 |
| 5.1.9 | Add tests: invalid markdown produces error notification, previous compiled version retained | 5.1.7 |
| 5.1.10 | Add tests: unchanged markdown skips recompilation | 5.1.5 |

### 5.2 Playbook Executor

| # | Task | Depends On |
|---|---|---|
| 5.2.1 | Create `PlaybookRun` DB table (Alembic migration) | — |
| 5.2.2 | Implement `PlaybookRunner` class: graph walker with conversation history | 5.2.1 |
| 5.2.3 | Implement node execution: build prompt + context, invoke Supervisor.chat() with history | 5.2.2, 0.4.1 |
| 5.2.4 | Implement transition evaluation: separate LLM call with condition list | 5.2.3 |
| 5.2.5 | Implement structured transitions: function-call expressions evaluated without LLM | 5.2.4 |
| 5.2.6 | Implement `summarize_before` node support (compress conversation history) | 5.2.3 |
| 5.2.7 | Implement token budget tracking per run (fail gracefully on exceed) | 5.2.3 |
| 5.2.8 | Implement global daily playbook token cap | 5.2.7 |
| 5.2.9 | Implement `PlaybookRun` persistence: conversation history, node trace, status | 5.2.2 |
| 5.2.10 | Implement run status transitions: running → completed/failed/paused/timed_out | 5.2.9 |
| 5.2.11 | Implement per-playbook and per-node `llm_config` override support | 5.2.3, 0.4.2 |
| 5.2.12 | Add tests: simple 3-node playbook executes start to finish | 5.2.3 |
| 5.2.13 | Add tests: transition evaluation chooses correct path | 5.2.4 |
| 5.2.14 | Add tests: token budget exceeded → run fails gracefully with preserved context | 5.2.7 |
| 5.2.15 | Add tests: PlaybookRun record contains correct node trace | 5.2.9 |

### 5.3 Event Integration

| # | Task | Depends On |
|---|---|---|
| 5.3.1 | Implement `PlaybookManager`: loads all compiled playbooks, maintains trigger → playbook mapping | 5.1.4 |
| 5.3.2 | Subscribe PlaybookManager to EventBus wildcard (or specific event types) | 5.3.1, 0.1.2 |
| 5.3.3 | Implement event-to-scope matching: events with project_id match project + system playbooks, events without match system only | 5.3.2 |
| 5.3.4 | Implement cooldown tracking per playbook | 5.3.1 |
| 5.3.5 | Implement concurrency limits (`max_concurrent_playbook_runs`) | 5.3.1 |
| 5.3.6 | Emit `playbook.run.completed` and `playbook.run.failed` events | 5.2.10 |
| 5.3.7 | Implement timer service: scan compiled playbooks for timer triggers, emit synthetic timer events | 5.3.1 |
| 5.3.8 | Add tests: event fires → correct playbook triggers | 5.3.3 |
| 5.3.9 | Add tests: cooldown prevents rapid re-triggering | 5.3.4 |
| 5.3.10 | Add tests: timer events fire at correct intervals | 5.3.7 |
| 5.3.11 | Add tests: playbook.run.completed triggers downstream playbook (composition) | 5.3.6, 0.1.2 |

### 5.4 Human-in-the-Loop

| # | Task | Depends On |
|---|---|---|
| 5.4.1 | Implement `wait_for_human` node: persist run state, pause execution | 5.2.9 |
| 5.4.2 | Implement Discord/Telegram notification for human review (with context summary) | 5.4.1 |
| 5.4.3 | Implement `human.review.completed` event handling: resume run from saved state | 5.4.1 |
| 5.4.4 | Implement timeout for paused runs (configurable, default 24h) | 5.4.1 |
| 5.4.5 | Implement `resume_playbook` command | 5.4.3 |
| 5.4.6 | Add tests: playbook pauses at human node, resumes after human input | 5.4.3 |
| 5.4.7 | Add tests: paused playbook times out and fails/transitions correctly | 5.4.4 |

### 5.5 Playbook Commands

| # | Task | Depends On |
|---|---|---|
| 5.5.1 | Implement `compile_playbook` command (manual trigger) | 5.1.2 |
| 5.5.2 | Implement `dry_run_playbook` command (simulate with mock event, no side effects) | 5.2.2 |
| 5.5.3 | Implement `show_playbook_graph` command (ASCII or mermaid output) | 5.1.4 |
| 5.5.4 | Implement `list_playbooks` command (all playbooks across scopes with status) | 5.3.1 |
| 5.5.5 | Implement `list_playbook_runs` command (recent runs with status/path) | 5.2.9 |
| 5.5.6 | Implement `inspect_playbook_run` command (full node trace, tokens, context) | 5.2.9 |
| 5.5.7 | Register all commands in CommandHandler via tool registry | 5.5.1–5.5.6 |
| 5.5.8 | Add tests: each command returns correct output | 5.5.7 |

### 5.6 Default Playbooks & Migration

| # | Task | Depends On |
|---|---|---|
| 5.6.1 | Write default `task-outcome.md` playbook | 5.1.2 |
| 5.6.2 | Write default `system-health-check.md` playbook (30m) | 5.1.2 |
| 5.6.3 | Write default `codebase-inspector.md` playbook (4h) | 5.1.2 |
| 5.6.4 | Write default `dependency-audit.md` playbook (24h) | 5.1.2 |
| 5.6.5 | Install default playbooks to vault on first run | 5.6.1–5.6.4 |
| 5.6.6 | Validate default playbooks produce equivalent results to current rules | 5.6.5, 5.3.1 |
| 5.6.7 | Remove default rules when playbook equivalents are validated | 5.6.6 |

> **Test checkpoint:** Full system test with playbooks running alongside hooks.
> Trigger `task.completed` — verify both the old hook AND the new playbook fire.
> Compare outputs. Verify token budgets tracked correctly. Test a human-in-the-loop
> playbook end-to-end via Discord. Verify timer-based playbooks fire on schedule.
> This is the biggest validation point in the entire roadmap.

---

## Phase 6: Self-Improvement Loop

Close the loop: agents learn from experience.

**Source:** [[self-improvement]]

### 6.1 Reflection Playbooks

| # | Task | Depends On |
|---|---|---|
| 6.1.1 | Write coding agent reflection playbook (`vault/agent-types/coding/playbooks/reflection.md`) | 5.1.2 |
| 6.1.2 | Write generic agent reflection playbook template | 6.1.1 |
| 6.1.3 | Implement reflection playbook trigger on `task.completed` for matching agent type | 5.3.3, 6.1.1 |
| 6.1.4 | Verify reflection playbook reads task records, extracts patterns, writes insights to agent-type memory | 6.1.3 |

### 6.2 Log Analysis

| # | Task | Depends On |
|---|---|---|
| 6.2.1 | Write log analysis playbook (`vault/system/playbooks/log-analysis.md`) | 5.1.2 |
| 6.2.2 | Implement log access tools for playbook use (read recent logs, filter by severity/date) | 6.2.1 |
| 6.2.3 | Verify log analysis writes operational insights to orchestrator memory | 6.2.1 |

### 6.3 Reference Stub Indexer

| # | Task | Depends On |
|---|---|---|
| 6.3.1 | Implement workspace spec/doc change detector (file watcher or git diff based) | 1.3.1 |
| 6.3.2 | Implement stub generator: read full doc, LLM-summarize, write to `vault/projects/{id}/references/` | 6.3.1 |
| 6.3.3 | Implement source_hash tracking to avoid regenerating unchanged stubs | 6.3.2 |
| 6.3.4 | Add tests: spec file changes → stub regenerated with updated summary | 6.3.2 |

### 6.4 Orchestrator Memory

| # | Task | Depends On |
|---|---|---|
| 6.4.1 | Implement startup scan of `vault/projects/*/README.md` → generate orchestrator summaries | 1.1.1, 2.2.5 |
| 6.4.2 | Wire README file watcher from Phase 1.3 to orchestrator re-summary | 1.3.5, 6.4.1 |
| 6.4.3 | Add tests: new project README → orchestrator summary created. README change → summary updated | 6.4.2 |

### 6.5 Memory Health

| # | Task | Depends On |
|---|---|---|
| 6.5.1 | Implement memory audit trail in frontmatter (created, source_task, last_retrieved, retrieval_count) | 2.1.12 |
| 6.5.2 | Implement `memory_health` command: collection sizes, growth rate, stale count | 6.5.1 |
| 6.5.3 | Implement stale memory detection (not retrieved in N days) | 6.5.1 |
| 6.5.4 | Add stale memory flagging to reflection playbook | 6.5.3, 6.1.1 |

> **Test checkpoint:** Let the system run for a day with reflection playbooks active.
> Check: did insights get extracted from completed tasks? Did duplicate insights get
> merged? Are retrieval counts incrementing? Are stale memories flagged? Is the
> orchestrator's project understanding current? This validates the entire
> self-improvement loop.

---

## Phase 7: Agent Coordination

Playbook-driven multi-agent workflows.

**Source:** [[agent-coordination]]

### 7.1 Workflow Infrastructure

| # | Task | Depends On |
|---|---|---|
| 7.1.1 | Add `workflow_id` nullable field to tasks table (Alembic migration) | — |
| 7.1.2 | Create `workflows` DB table (Alembic migration) | — |
| 7.1.3 | Implement Workflow CRUD queries (create, update status, add task, get by ID) | 7.1.2 |
| 7.1.4 | Implement `workflow.stage.completed` event emission | 7.1.3 |
| 7.1.5 | Add tests: workflow creation, task association, status transitions | 7.1.3 |

### 7.2 Coordination Commands

| # | Task | Depends On |
|---|---|---|
| 7.2.1 | Extend `create_task` command with `agent_type`, `affinity_agent_id`, `workspace_mode` parameters | 7.1.1 |
| 7.2.2 | Implement `set_project_constraint` command (exclusive access, max agents by type, pause) | — |
| 7.2.3 | Implement `release_project_constraint` command | 7.2.2 |
| 7.2.4 | Implement constraint enforcement in scheduler (check before assignment) | 7.2.2 |
| 7.2.5 | Add tests: constraint blocks assignment, release allows it | 7.2.4 |

### 7.3 Agent Affinity

| # | Task | Depends On |
|---|---|---|
| 7.3.1 | Add `affinity_agent_id` and `affinity_reason` fields to tasks | 7.2.1 |
| 7.3.2 | Implement scheduler affinity logic: prefer idle affinity agent, bounded wait, fallback | 7.3.1 |
| 7.3.3 | Implement agent type matching: task's `agent_type` field matched against agent's type | 7.3.1 |
| 7.3.4 | Add tests: affinity agent preferred when idle, fallback when busy too long | 7.3.2 |
| 7.3.5 | Add tests: agent type mismatch prevents assignment | 7.3.3 |

### 7.4 Workspace Modes

| # | Task | Depends On |
|---|---|---|
| 7.4.1 | Add `lock_mode` field to workspace acquisition (default: `exclusive`) | — |
| 7.4.2 | Implement `branch-isolated` lock mode (multiple agents, same repo, different branches) | 7.4.1 |
| 7.4.3 | Implement git mutex for shared operations (fetch, gc) in branch-isolated mode | 7.4.2 |
| 7.4.4 | Add tests: two agents with branch-isolated mode work concurrently on separate branches | 7.4.2 |
| 7.4.5 | Add tests: exclusive mode still prevents concurrent access | 7.4.1 |

### 7.5 Coordination Playbooks

| # | Task | Depends On |
|---|---|---|
| 7.5.1 | Write `feature-pipeline.md` default coordination playbook | 5.1.2, 7.1.3 |
| 7.5.2 | Write `bugfix-pipeline.md` default coordination playbook | 5.1.2, 7.1.3 |
| 7.5.3 | Write `review-cycle.md` default coordination playbook | 5.1.2, 7.1.3 |
| 7.5.4 | Write `exploration.md` default coordination playbook | 5.1.2, 7.1.3 |
| 7.5.5 | Implement long-running playbook support (event-triggered resumption across stages) | 5.4.1, 7.1.4 |
| 7.5.6 | Add tests: feature pipeline creates coding → review + QA → merge task chain | 7.5.1 |
| 7.5.7 | Add tests: review feedback creates fix task with affinity to original agent | 7.5.1, 7.3.2 |
| 7.5.8 | Add tests: exploration creates parallel tasks, reviewer depends on all | 7.5.4 |

> **Test checkpoint:** End-to-end coordination test: create a FEATURE task. Verify
> the feature-pipeline playbook triggers, creates a coding task, waits for PR, creates
> review + QA tasks (running concurrently via dependency DAG), handles review feedback
> cycle, and completes the workflow. Verify the scheduler respects agent affinity and
> the dependency graph drives concurrency correctly.

---

## Phase 8: Hook Engine Deprecation

Remove the old system once playbooks are validated.

| # | Task | Depends On |
|---|---|---|
| 8.1 | Migrate all user-created active rules to playbooks (generate playbook markdown from rule files) | 5.6.6 |
| 8.2 | Migrate passive rules to vault memory files (in appropriate agent-type or project scope) | 3.1.1 |
| 8.3 | Redirect hook commands to playbook equivalents (`list_hooks` → `list_playbooks`, etc.) | 5.5.7 |
| 8.4 | Remove `HookEngine`, `RuleManager`, and related code | 8.1, 8.2, 8.3 |
| 8.5 | Remove hook/rule DB tables (Alembic migration) | 8.4 |
| 8.6 | Remove `src/memory.py` MemoryManager and v1 memory plugin | 2.2.14 |
| 8.7 | Update all specs to remove "future evolution" callouts (they're now current) | 8.4 |

> **Final test checkpoint:** Full regression test. Every feature that worked with
> hooks still works with playbooks. Memory operations all route through v2 plugin.
> No references to hook engine in active code paths.

---

## Summary

| Phase | Tasks | Key Deliverable | Depends On |
|---|---|---|---|
| **0** | 21 | Prerequisite refactors (EventBus, git events, Supervisor flexibility) | — |
| **1** | 18 | Vault structure + file watcher | — |
| **2** | 33 | memsearch fork + memory plugin v2 | Phase 1 (partial) |
| **3** | 24 | Memory scoping, tiers, overrides, dedup | Phase 2 |
| **4** | 17 | Profiles as markdown | Phase 1, Phase 3 (partial) |
| **5** | 40 | Playbook system (compiler, executor, events, HITL, commands) | Phase 0, Phase 1 |
| **6** | 14 | Self-improvement loop | Phase 3, Phase 5 |
| **7** | 22 | Agent coordination (workflows, affinity, workspace modes) | Phase 5 |
| **8** | 7 | Hook engine deprecation | Phase 5, Phase 6 |
| **Total** | **196** | | |

### Parallelism Opportunities

Phases 0 and 1 can run in parallel (independent).
Phase 2 can start as soon as Phase 1.3 (file watcher) lands.
Phase 4 can start as soon as Phase 1.3 + Phase 3.1 land.
Phase 5 can start as soon as Phase 0 + Phase 1 land (independent of memory work).
Phase 6 requires both Phase 3 and Phase 5.
Phase 7 requires Phase 5.

```
Phase 0 ──────────────────────────► Phase 5 ──► Phase 7
                                        │           │
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 6    Phase 8
                              │
                          Phase 4
```
