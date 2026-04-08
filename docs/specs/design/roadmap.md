---
tags: [design, roadmap, planning]
---

# Implementation Roadmap

**Related:** [[guiding-design-principles]], [[playbooks]], [[vault]], [[memory-plugin]],
[[memory-scoping]], [[profiles]], [[self-improvement]], [[agent-coordination]]

This roadmap breaks the design specs into phased work with explicit dependencies,
spec backlinks, and testing checkpoints. Phases are ordered so each builds on the
last. Within each phase, tasks are grouped into workstreams that can run in parallel
where dependencies allow.

---

## Phase 0: Prerequisite Refactors

These changes prepare the existing codebase for the new systems. They are
independently valuable and can ship without any design spec work landing.

**Source:** [[playbooks#17. Prerequisite Refactors]]

### 0.1 EventBus Payload Filtering

Extend EventBus subscriptions to support dict-based payload filters. Required for
cross-playbook composition ([[playbooks#10. Composability|playbook composability]]).

**Spec:** [[playbooks#17. Prerequisite Refactors]] — EventBus Payload Filtering
**Existing code:** `src/event_bus.py` ([[specs/event-bus]])

| # | Task | Depends On |
|---|---|---|
| 0.1.1 | Add `filter` parameter to `EventBus.subscribe()` accepting `dict[str, Any]` | — |
| 0.1.2 | Implement filter matching logic (all conditions must match event payload fields) | 0.1.1 |
| 0.1.3 | Add tests: filtered subscription receives only matching events | 0.1.2 |
| 0.1.4 | Add tests: unfiltered subscriptions continue to work (backward compat) | 0.1.2 |
| 0.1.5 | Add tests: events missing filter fields are skipped (not matched) | 0.1.2 |

> **Test checkpoint:** Run full existing event-bus test suite + new filter tests.
> Verify zero regressions in hook engine and orchestrator event handling.

### 0.2 Event Schema Registry

Lightweight validation for event payloads. Prevents silent scope mismatches from
missing `project_id` fields.

**Spec:** [[playbooks#17. Prerequisite Refactors]] — Event Schema Registry
**Existing code:** event types defined across `src/orchestrator.py`, `src/event_bus.py`,
`src/file_watcher.py`, `src/notifications/events.py`

| # | Task | Depends On |
|---|---|---|
| 0.2.1 | Define `EVENT_SCHEMAS` dict with required/optional fields per event type | — |
| 0.2.2 | Implement `validate_event(event_type, payload)` function | 0.2.1 |
| 0.2.3 | Wire validation into `EventBus.emit()` (warn in prod, error in dev) | 0.2.2 |
| 0.2.4 | Add schemas for all existing event types (task.*, note.*, file.*, plugin.*, config.*) | 0.2.1 |
| 0.2.5 | Add schemas for new event types (git.*, playbook.*, human.*, workflow.*) | 0.2.1 |
| 0.2.6 | Add tests: valid events pass, missing required fields warn/error | 0.2.3 |

### 0.3 GitManager Event Emission

Add event emission to existing git operations. Enables code quality gates and
commit-triggered playbooks.

**Spec:** [[playbooks#7. Event System]] — New Events Needed, [[playbooks#17. Prerequisite Refactors]] — GitManager Event Emission
**Existing code:** `src/git/manager.py` ([[specs/git]])

| # | Task | Depends On |
|---|---|---|
| 0.3.1 | Define schemas for `git.commit`, `git.push`, `git.pr.created` events | 0.2.1 |
| 0.3.2 | Emit `git.commit` from `GitManager.acommit_all()` with commit_hash, branch, changed_files, message, project_id, agent_id | — |
| 0.3.3 | Emit `git.push` from `GitManager.apush_branch()` with branch, remote, commit_range, project_id | — |
| 0.3.4 | Emit `git.pr.created` from `GitManager.create_pr()` with pr_url, branch, title, project_id | — |
| 0.3.5 | Add tests: verify events emitted with correct payloads after each git operation | 0.3.2, 0.3.3, 0.3.4 |

> **Test checkpoint:** Create a task, let an agent commit + push + create PR.
> Verify all three git events fire with correct payloads in the event log.

### 0.4 Supervisor Configuration Flexibility

Enable per-call model and tool overrides. Required for playbook per-node
`llm_config` and transition evaluation with cheaper models.

**Spec:** [[playbooks#6. Execution Model]] — Customizable Agent Configuration,
[[playbooks#17. Prerequisite Refactors]] — Supervisor Configuration Flexibility
**Existing code:** `src/supervisor.py` ([[specs/supervisor]])

| # | Task | Depends On |
|---|---|---|
| 0.4.1 | Add `llm_config` optional parameter to `Supervisor.chat()` | — |
| 0.4.2 | Implement chat provider swap based on `llm_config` within a call | 0.4.1 |
| 0.4.3 | Add `tool_overrides` parameter to `Supervisor.chat()` for restricting available tool set | 0.4.1 |
| 0.4.4 | Add tests: verify model override produces response from correct provider | 0.4.2 |
| 0.4.5 | Add tests: verify tool restriction prevents unauthorized tool calls | 0.4.3 |

### 0.5 Task Records Migration

Move task records out of the memory search path. Stops task files from polluting
memory retrieval results.

**Spec:** [[vault#6. Migration Path]] — Phase 1, [[playbooks#17. Prerequisite Refactors]] — Task Records Migration
**Existing code:** `src/memory.py` (task record storage paths)

| # | Task | Depends On |
|---|---|---|
| 0.5.1 | Create `~/.agent-queue/tasks/{project_id}/` directory structure | — |
| 0.5.2 | Update task record write path in MemoryManager to use new location | 0.5.1 |
| 0.5.3 | Write migration script to move existing `memory/*/tasks/` to `tasks/*/` | 0.5.1 |
| 0.5.4 | Update memory indexer to exclude the old tasks path | 0.5.2 |
| 0.5.5 | Re-index existing project memory collections (without task files) | 0.5.4 |
| 0.5.6 | Add tests: task records write to new path, memory search no longer returns task files | 0.5.5 |

> **Test checkpoint:** Full system test — create tasks, complete them, verify records
> appear in new location. Memory search should return cleaner results without task noise.

---

## Phase 1: Vault Structure

Create the vault directory layout and the unified file watcher. This is the
foundation that all other phases depend on.

**Source:** [[vault#2. Directory Layout]], [[vault#5. Obsidian Integration]]

### 1.1 Vault Directory Creation

**Spec:** [[vault#2. Directory Layout]]

| # | Task | Depends On |
|---|---|---|
| 1.1.1 | Create vault directory structure at `~/.agent-queue/vault/` with all subdirectories (system/, orchestrator/, agent-types/, projects/, templates/) per [[vault#2. Directory Layout]] | — |
| 1.1.2 | Add vault path constants to `AppConfig` ([[specs/config]]) | — |
| 1.1.3 | Create `vault_manager.py` module for vault path resolution and directory creation | 1.1.1, 1.1.2 |
| 1.1.4 | Wire vault initialization into orchestrator startup ([[specs/orchestrator]]) | 1.1.3 |

### 1.2 Content Migration

**Spec:** [[vault#6. Migration Path]] — Phase 1

| # | Task | Depends On |
|---|---|---|
| 1.2.1 | Move existing `.obsidian/` config from `memory/` to `vault/` | 1.1.1 |
| 1.2.2 | Move existing rule files from `memory/*/rules/` to `vault/system/playbooks/` (or project playbooks per [[playbooks#8. Scoping]]) | 1.1.1 |
| 1.2.3 | Move existing notes from `notes/` to `vault/projects/*/notes/` | 1.1.1 |
| 1.2.4 | Copy existing project memory files to `vault/projects/*/memory/` | 1.1.1 |
| 1.2.5 | Write migration script that handles all moves idempotently | 1.2.1–1.2.4 |
| 1.2.6 | Add startup check: if old paths exist and vault is empty, run migration | 1.2.5 |

### 1.3 Unified Vault File Watcher

**Spec:** [[playbooks#17. Prerequisite Refactors]] — Unified Vault File Watcher,
[[vault#5. Obsidian Integration]] (file changes drive re-indexing)

| # | Task | Depends On |
|---|---|---|
| 1.3.1 | Implement `VaultWatcher` class using existing `FileWatcher` pattern ([[specs/hooks]]) | 1.1.1 |
| 1.3.2 | Implement path-based dispatch: `*/playbooks/*.md` → playbook compilation handler | 1.3.1 |
| 1.3.3 | Implement path-based dispatch: `*/profile.md` → profile sync handler | 1.3.1 |
| 1.3.4 | Implement path-based dispatch: `*/memory/**/*.md` → memory re-index handler | 1.3.1 |
| 1.3.5 | Implement path-based dispatch: `projects/*/README.md` → orchestrator summary handler | 1.3.1 |
| 1.3.6 | Implement path-based dispatch: `*/overrides/*.md` → override re-index handler | 1.3.1 |
| 1.3.7 | Implement path-based dispatch: `*/facts.md` → KV sync handler | 1.3.1 |
| 1.3.8 | Wire `VaultWatcher` into orchestrator startup and tick loop | 1.3.1 |
| 1.3.9 | Add tests: file changes in each path trigger correct handler | 1.3.2–1.3.7 |
| 1.3.10 | Add tests: debounce prevents rapid re-triggering of handlers | 1.3.8 |

> **Test checkpoint:** Create vault structure, edit files in each location, verify
> the correct handler fires. Edit a profile.md, verify change is detected. Edit
> a memory file, verify re-index triggered. This is the foundation — it must be solid.

---

## Phase 2: memsearch Fork & Memory Plugin v2

Fork memsearch, add KV + temporal + topic support, build the new plugin.

**Source:** [[memory-plugin#3. New Architecture]], [[memory-plugin#5. memsearch Fork]],
[[memory-plugin#6. Collection Schema]], [[memory-plugin#7. Milvus Backend Topology]]

### 2.1 memsearch Fork

**Spec:** [[memory-plugin#5. memsearch Fork]], [[memory-plugin#6. Collection Schema]]

| # | Task | Depends On |
|---|---|---|
| 2.1.1 | Fork [zilliztech/memsearch](https://github.com/zilliztech/memsearch) to internal repo | — |
| 2.1.2 | Implement unified collection schema per [[memory-plugin#6. Collection Schema]] (entry_type, content, original, kv fields, valid_from/to, topic, tags, updated_at) | 2.1.1 |
| 2.1.3 | Implement KV insert/query methods (scalar-only, no vector search) per [[memory-plugin#6. Collection Schema]] KV Queries | 2.1.2 |
| 2.1.4 | Implement temporal insert/query methods (valid_from/valid_to windowed lookups) per [[memory-plugin#6. Collection Schema]] Temporal Queries | 2.1.2 |
| 2.1.5 | Implement temporal fact lifecycle (close old window, open new on update) per [[memory-plugin#6. Collection Schema]] Temporal Fact Lifecycle | 2.1.4 |
| 2.1.6 | Implement historical "as-of" query method | 2.1.4 |
| 2.1.7 | Implement topic field support (scalar filter before vector search) per [[memory-scoping#3. Topic Filtering]] | 2.1.2 |
| 2.1.8 | Implement topic filter fallback: auto-widen to unfiltered when < 3 results per [[memory-scoping#3. Topic Filtering]] | 2.1.7 |
| 2.1.9 | Implement multi-collection parallel search with weighted merging per [[memory-scoping#4. Scope Hierarchy]] | 2.1.2 |
| 2.1.10 | Implement scope-aware collection naming (`aq_system`, `aq_agenttype_*`, `aq_project_*`) per [[memory-plugin#7. Milvus Backend Topology]] | 2.1.2 |
| 2.1.11 | Implement tag-based cross-collection search per [[memory-plugin#7. Milvus Backend Topology]] Tag-Based Cross-Scope Discovery | 2.1.2 |
| 2.1.12 | Implement `original` field storage (full content alongside summary embedding) per [[memory-scoping#9. Summary + Original Pattern]] | 2.1.2 |
| 2.1.13 | Implement retrieval tracking (update `retrieval_count`, `last_retrieved`) per [[self-improvement#6. Memory Health & Observability]] | 2.1.2 |
| 2.1.14 | Implement embedding model version tracking per collection for future re-indexing per [[memory-plugin#8. Open Questions]] | 2.1.2 |
| 2.1.15 | Add tests: KV insert/query round-trip | 2.1.3 |
| 2.1.16 | Add tests: temporal insert, update (close/open window), as-of query | 2.1.5, 2.1.6 |
| 2.1.17 | Add tests: topic-filtered search vs. unfiltered, fallback on < 3 results | 2.1.7, 2.1.8 |
| 2.1.18 | Add tests: multi-collection weighted merge produces correct ranking | 2.1.9 |
| 2.1.19 | Add tests: cross-collection tag search | 2.1.11 |
| 2.1.20 | Add tests: retrieval tracking increments on search results | 2.1.13 |

> **Test checkpoint:** Run the full memsearch fork test suite against Milvus Lite.
> Verify all new features work in isolation before integrating with the plugin.

### 2.2 Memory Plugin v2 Skeleton

**Spec:** [[memory-plugin#3. New Architecture]], [[memory-plugin#4. Why a Plugin (Not Core)]],
[[memory-scoping#7. Agent Memory Tools (MCP)]]

| # | Task | Depends On |
|---|---|---|
| 2.2.1 | Create `src/plugins/internal/memory_v2.py` plugin skeleton implementing InternalPlugin per [[specs/plugin-system]] | — |
| 2.2.2 | Register plugin with PluginRegistry, coexisting with v1 during transition | 2.2.1 |
| 2.2.3 | Implement `MemoryService` v2 protocol wrapping the memsearch fork per [[memory-plugin#3. New Architecture]] | 2.1.10, 2.2.1 |
| 2.2.4 | Implement MCP tool: `memory_search` (semantic search with optional topic filter) per [[memory-scoping#7. Agent Memory Tools (MCP)]] | 2.2.3 |
| 2.2.5 | Implement MCP tool: `memory_save` (with dedup, summary/original, topic auto-detect) per [[memory-scoping#8. memory_save Flow]] | 2.2.3 |
| 2.2.6 | Implement topic auto-detection in `memory_save` (infer from content + task context) per [[memory-scoping#3. Topic Filtering]] | 2.2.5 |
| 2.2.7 | Implement MCP tool: `memory_list` (browse memories in a scope) | 2.2.3 |
| 2.2.8 | Implement MCP tool: `memory_recall` (KV lookup with scope resolution) per [[memory-scoping#6. Multi-Scope Query]] | 2.2.3 |
| 2.2.9 | Implement MCP tool: `memory_store` (KV write to scope + vault fact file update) | 2.2.3 |
| 2.2.10 | Implement MCP tool: `memory_list_facts` (list KV entries by scope/namespace) | 2.2.3 |
| 2.2.11 | Implement MCP tool: `memory_get` (unified auto-routing: KV first, then semantic) per [[memory-scoping#7. Agent Memory Tools (MCP)]] | 2.2.8, 2.2.4 |
| 2.2.12 | Implement `full=true` parameter on `memory_get` to return original instead of summary per [[memory-scoping#9. Summary + Original Pattern]] | 2.2.11 |
| 2.2.13 | Implement facts.md parser (key:value pairs under markdown headings → KV entries) per [[memory-plugin#7. Milvus Backend Topology]] Fact Files | 2.2.3 |
| 2.2.14 | Implement facts.md writer (KV changes → update vault fact file bidirectionally) | 2.2.13 |
| 2.2.15 | Wire facts.md file watcher handler from Phase 1.3 to facts.md parser | 1.3.7, 2.2.13 |
| 2.2.16 | Add tests: each MCP tool round-trip (save/search, store/recall, list) | 2.2.4–2.2.11 |
| 2.2.17 | Add tests: facts.md parse → KV insert → recall returns correct value | 2.2.13, 2.2.8 |
| 2.2.18 | Add tests: memory_get routes to KV for exact matches, semantic for fuzzy | 2.2.11 |
| 2.2.19 | Add tests: topic auto-detection assigns reasonable topics | 2.2.6 |

> **Test checkpoint:** End-to-end: an agent saves an insight via MCP, a second agent
> searches and finds it. An agent stores a fact, another agent recalls it. Both the
> vault markdown files and the Milvus collections are consistent.

---

## Phase 3: Memory Scoping & Tiers

Build the scope hierarchy, tiered loading, and override model.

**Source:** [[memory-scoping]]

### 3.1 Scope Resolution

**Spec:** [[memory-scoping#4. Scope Hierarchy]], [[memory-scoping#6. Multi-Scope Query]]

| # | Task | Depends On |
|---|---|---|
| 3.1.1 | Implement scope resolver: given (agent_type, project_id), return ordered collection list with weights per [[memory-scoping#4. Scope Hierarchy]] | 2.1.10 |
| 3.1.2 | Create per-agent-type collections on first profile creation | 3.1.1 |
| 3.1.3 | Create system-level collection on startup | 3.1.1 |
| 3.1.4 | Create orchestrator collection on startup | 3.1.1 |
| 3.1.5 | Migrate existing per-project collections to new naming convention | 3.1.1 |
| 3.1.6 | Implement first-match-wins KV scope resolution (project → agent-type → system) per [[memory-scoping#6. Multi-Scope Query]] | 3.1.1 |
| 3.1.7 | Implement weighted merge for semantic search across scopes (project weight=1.0, agent-type=0.7, system=0.4) per [[memory-scoping#6. Multi-Scope Query]] | 3.1.1 |
| 3.1.8 | Add tests: scope resolution returns correct collections in correct order | 3.1.1 |
| 3.1.9 | Add tests: KV lookup finds project-level fact, falls through to system when not found | 3.1.6 |
| 3.1.10 | Add tests: semantic search merges results from multiple scopes with correct weighting | 3.1.7 |

### 3.2 Override Model

**Spec:** [[memory-scoping#5. Override Model]]

| # | Task | Depends On |
|---|---|---|
| 3.2.1 | Implement override file indexing (`vault/projects/{id}/overrides/{type}.md` into project collection with highest weight) per [[memory-scoping#5. Override Model]] | 3.1.1 |
| 3.2.2 | Wire override file watcher handler from Phase 1.3 | 1.3.6, 3.2.1 |
| 3.2.3 | Implement override injection into agent context alongside base [[profiles|profile]] | 3.2.1 |
| 3.2.4 | Add tests: override content appears in search results with highest weight | 3.2.1 |
| 3.2.5 | Add tests: override for agent-type X doesn't appear in agent-type Y searches | 3.2.1 |

### 3.3 Memory Tiers

**Spec:** [[memory-scoping#2. Memory Tiers (L0–L3)]]

| # | Task | Depends On |
|---|---|---|
| 3.3.1 | Implement L0 injection: extract `## Role` from [[profiles|profile.md]] into agent system prompt (~50 tokens) | 2.2.3 |
| 3.3.2 | Implement L1 injection: eager-load project + agent-type facts.md KV entries at task start (~200 tokens) | 2.2.8, 3.1.6 |
| 3.3.3 | Implement L2 topic detection from task description/context per [[memory-scoping#3. Topic Filtering]] | 2.1.7 |
| 3.3.4 | Implement L2 topic-filtered memory loading when topic is detected (~500 tokens) | 3.3.3 |
| 3.3.5 | Wire L0 + L1 into task execution path (adapter/prompt context building, [[specs/prompt-builder]]) | 3.3.1, 3.3.2 |
| 3.3.6 | Wire L2 into task execution path (on-demand when topic emerges) | 3.3.4 |
| 3.3.7 | Add tests: L0+L1 present in every task context (~250 tokens baseline) | 3.3.5 |
| 3.3.8 | Add tests: L2 topic memories appear when topic is relevant, absent when not | 3.3.6 |
| 3.3.9 | Add tests: L3 search (via `memory_search` tool) still works and returns cross-topic results | 3.3.6 |

### 3.4 Deduplication & Summary

**Spec:** [[memory-scoping#8. memory_save Flow]], [[memory-scoping#9. Summary + Original Pattern]]

| # | Task | Depends On |
|---|---|---|
| 3.4.1 | Implement similarity-based dedup in `memory_save` (>0.95 timestamp update, 0.8-0.95 LLM merge, <0.8 create new) per [[memory-scoping#8. memory_save Flow]] | 2.2.5 |
| 3.4.2 | Implement LLM merge for 0.8-0.95 similarity: combine content, prefer newer on contradiction, preserve tags per [[memory-scoping#8. memory_save Flow]] | 3.4.1 |
| 3.4.3 | Implement summary generation for long memories (>200 tokens → summarize for embedding, keep original) per [[memory-scoping#9. Summary + Original Pattern]] | 2.1.12, 2.2.5 |
| 3.4.4 | Add tests: duplicate save is deduplicated (timestamp update only) | 3.4.1 |
| 3.4.5 | Add tests: similar save triggers LLM merge, result combines both | 3.4.2 |
| 3.4.6 | Add tests: distinct save creates new file | 3.4.1 |
| 3.4.7 | Add tests: long content is summarized for search but original is retrievable via `full=true` | 3.4.3, 2.2.12 |

> **Test checkpoint:** Full integration test: create an agent with a [[profiles|profile]],
> set up project facts, create [[memory-scoping#5. Override Model|overrides]]. Start a
> task — verify L0 (role) and L1 (facts) are in the context. Save several insights with
> topics. Start another task on the same topic — verify L2 topic memories appear. Search
> across topics — verify L3 returns cross-topic results with correct scope weighting.

---

## Phase 4: Profiles as Markdown

Move profiles from DB-only to markdown source of truth in the [[vault]].

**Source:** [[profiles]]

### 4.1 Profile Parser & Sync

**Spec:** [[profiles#2. Hybrid Format]], [[profiles#3. Sync Model]]

| # | Task | Depends On |
|---|---|---|
| 4.1.1 | Implement markdown profile parser: extract JSON blocks from `## Config`, `## Tools`, `## MCP Servers` per [[profiles#2. Hybrid Format]] | — |
| 4.1.2 | Implement English section extractor for `## Role`, `## Rules`, `## Reflection` | 4.1.1 |
| 4.1.3 | Implement JSON validation for Config block (model, permission_mode, max_tokens_per_task) | 4.1.1 |
| 4.1.4 | Implement JSON validation for Tools block (tool names checked against [[specs/tiered-tools|tool registry]], warn on unknown) | 4.1.1 |
| 4.1.5 | Implement JSON validation for MCP Servers block (command, args, env structure) | 4.1.1 |
| 4.1.6 | Implement profile → DB sync (parsed fields → `agent_profiles` table upsert) per [[profiles#3. Sync Model]] | 4.1.1, 4.1.2 |
| 4.1.7 | Wire profile.md file watcher handler from Phase 1.3 to parser + sync | 1.3.3, 4.1.6 |
| 4.1.8 | Implement error handling: bad JSON → sync fails, previous config retained, notification sent per [[profiles#3. Sync Model]] | 4.1.6 |
| 4.1.9 | Update chat/dashboard profile commands to write to markdown file instead of DB ([[specs/command-handler]]) | 4.1.6 |
| 4.1.10 | Add tests: valid profile.md parses correctly and syncs all fields to DB | 4.1.6 |
| 4.1.11 | Add tests: invalid JSON in profile.md triggers failure notification, DB unchanged | 4.1.8 |
| 4.1.12 | Add tests: edit profile.md in vault, verify DB updates within file watcher cycle | 4.1.7 |

### 4.2 Profile Migration

**Spec:** [[vault#6. Migration Path]] — Phase 4

| # | Task | Depends On |
|---|---|---|
| 4.2.1 | Write migration script: read existing DB profiles → generate markdown files in vault per [[profiles#2. Hybrid Format]] | 4.1.1 |
| 4.2.2 | Create default profile templates in `vault/templates/` | — |
| 4.2.3 | Create orchestrator profile.md (the orchestrator is its own agent type per [[self-improvement#5. Orchestrator Memory]]) | 4.2.2 |
| 4.2.4 | Add startup check: if DB profiles exist but no vault markdown, run migration | 4.2.1 |

### 4.3 Starter Knowledge Packs

**Spec:** [[profiles#4. Starter Knowledge Packs]]

| # | Task | Depends On |
|---|---|---|
| 4.3.1 | Create starter knowledge files for `coding` agent type (common pitfalls, git conventions) | — |
| 4.3.2 | Create starter knowledge files for `code-review` agent type (review checklist) | — |
| 4.3.3 | Create starter knowledge files for `qa` agent type (testing patterns) | — |
| 4.3.4 | Implement knowledge pack copy on first profile.md creation (detect new profile, copy matching templates, tag `#starter`) per [[profiles#4. Starter Knowledge Packs]] | 4.1.7, 4.3.1 |
| 4.3.5 | Add tests: new agent type profile triggers knowledge pack copy, files tagged `#starter` | 4.3.4 |

> **Test checkpoint:** Create a new agent profile via chat command. Verify: markdown
> file appears in vault, DB row syncs, starter knowledge pack copied. Edit the
> profile.md in Obsidian — verify DB updates. Intentionally break JSON in profile —
> verify graceful failure with notification.

---

## Phase 5: Playbook System

The core new automation system replacing rules + hooks.

**Source:** [[playbooks]]

### 5.1 Playbook Compilation

**Spec:** [[playbooks#4. Authoring Model]] — LLM Compilation, [[playbooks#5. Compiled Format (JSON Schema)]]

| # | Task | Depends On |
|---|---|---|
| 5.1.1 | Define playbook JSON schema as a Python dataclass or JSON Schema file per [[playbooks#5. Compiled Format (JSON Schema)]] (node fields, transition fields, top-level fields) | — |
| 5.1.2 | Implement `PlaybookCompiler` class: reads markdown + frontmatter, invokes LLM with schema, validates output | 5.1.1 |
| 5.1.3 | Implement graph validation: entry node exists, all transitions reference valid nodes, no unreachable nodes, cycles have exit conditions per [[playbooks#19. Open Questions]] #6 | 5.1.1 |
| 5.1.4 | Implement compiled JSON storage in `~/.agent-queue/compiled/` with scope-mirrored directory structure per [[playbooks#8. Scoping]] Storage | 5.1.2 |
| 5.1.5 | Implement source_hash change detection (skip recompilation when unchanged) per [[playbooks#4. Authoring Model]] | 5.1.4 |
| 5.1.6 | Wire playbook file watcher handler from Phase 1.3 to compiler | 1.3.2, 5.1.2 |
| 5.1.7 | Implement compilation error handling (keep previous version active, surface error notification) per [[playbooks#4. Authoring Model]] | 5.1.2 |
| 5.1.8 | Add tests: sample markdown compiles to valid JSON matching schema | 5.1.2 |
| 5.1.9 | Add tests: invalid markdown produces error notification, previous compiled version retained | 5.1.7 |
| 5.1.10 | Add tests: unchanged markdown skips recompilation | 5.1.5 |
| 5.1.11 | Add tests: graph validation catches unreachable nodes, missing entry, invalid transition targets | 5.1.3 |

### 5.2 Playbook Executor

**Spec:** [[playbooks#6. Execution Model]]

| # | Task | Depends On |
|---|---|---|
| 5.2.1 | Create `PlaybookRun` DB table (Alembic migration) per [[playbooks#6. Execution Model]] Run Persistence | — |
| 5.2.2 | Implement `PlaybookRunner` class: graph walker with conversation history per [[playbooks#6. Execution Model]] Context via Conversation History | 5.2.1 |
| 5.2.3 | Implement node execution: build prompt + context, invoke `Supervisor.chat()` with accumulated history per [[playbooks#6. Execution Model]] | 5.2.2, 0.4.1 |
| 5.2.4 | Implement transition evaluation: separate LLM call with condition list per [[playbooks#6. Execution Model]] Transition Evaluation | 5.2.3 |
| 5.2.5 | Implement structured transitions: function-call expressions evaluated without LLM per [[playbooks#6. Execution Model]] Transition Evaluation | 5.2.4 |
| 5.2.6 | Implement `summarize_before` node support (compress conversation history) per [[playbooks#6. Execution Model]] Context Size Management | 5.2.3 |
| 5.2.7 | Implement token budget tracking per run (fail gracefully on exceed) per [[playbooks#6. Execution Model]] Token Budget | 5.2.3 |
| 5.2.8 | Implement global daily playbook token cap (`max_daily_playbook_tokens` in config) per [[playbooks#6. Execution Model]] Token Budget | 5.2.7 |
| 5.2.9 | Implement `PlaybookRun` persistence: conversation history, node trace, status per [[playbooks#6. Execution Model]] Run Persistence | 5.2.2 |
| 5.2.10 | Implement run status transitions: running → completed/failed/paused/timed_out per [[playbooks#6. Execution Model]] | 5.2.9 |
| 5.2.11 | Implement per-playbook and per-node `llm_config` override support per [[playbooks#6. Execution Model]] Customizable Agent Configuration | 5.2.3, 0.4.2 |
| 5.2.12 | Implement playbook version pinning: in-flight runs continue with old version when recompiled per [[playbooks#19. Open Questions]] #3 | 5.2.9 |
| 5.2.13 | Add tests: simple 3-node playbook executes start to finish | 5.2.3 |
| 5.2.14 | Add tests: branching transition evaluation chooses correct path | 5.2.4 |
| 5.2.15 | Add tests: structured transition evaluates without LLM call | 5.2.5 |
| 5.2.16 | Add tests: token budget exceeded → run fails gracefully with preserved context | 5.2.7 |
| 5.2.17 | Add tests: PlaybookRun record contains correct node trace and conversation | 5.2.9 |

### 5.3 Event Integration

**Spec:** [[playbooks#7. Event System]], [[playbooks#8. Scoping]] — Scope Resolution

| # | Task | Depends On |
|---|---|---|
| 5.3.1 | Implement `PlaybookManager`: loads all compiled playbooks, maintains trigger → playbook mapping | 5.1.4 |
| 5.3.2 | Subscribe PlaybookManager to EventBus with payload filtering per [[playbooks#10. Composability]] Event Payload Filtering | 5.3.1, 0.1.2 |
| 5.3.3 | Implement event-to-scope matching per [[playbooks#7. Event System]] Event-to-Scope Matching: events with project_id match project + system playbooks, events without match system only | 5.3.2 |
| 5.3.4 | Implement cooldown tracking per playbook per [[playbooks#6. Execution Model]] Concurrency | 5.3.1 |
| 5.3.5 | Implement concurrency limits (`max_concurrent_playbook_runs`) per [[playbooks#6. Execution Model]] Concurrency | 5.3.1 |
| 5.3.6 | Emit `playbook.run.completed` and `playbook.run.failed` events per [[playbooks#7. Event System]] | 5.2.10 |
| 5.3.7 | Implement timer service per [[playbooks#7. Event System]] Timer Service: scan compiled playbooks for timer triggers, emit synthetic timer events, minimum 1m interval | 5.3.1 |
| 5.3.8 | Add tests: event fires → correct playbook triggers based on scope | 5.3.3 |
| 5.3.9 | Add tests: cooldown prevents rapid re-triggering | 5.3.4 |
| 5.3.10 | Add tests: timer events fire at correct intervals | 5.3.7 |
| 5.3.11 | Add tests: `playbook.run.completed` with payload filter triggers downstream playbook (composition chain) | 5.3.6, 0.1.2 |

### 5.4 Human-in-the-Loop

**Spec:** [[playbooks#9. Human-in-the-Loop]]

| # | Task | Depends On |
|---|---|---|
| 5.4.1 | Implement `wait_for_human` node: persist run state to DB, pause execution per [[playbooks#9. Human-in-the-Loop]] Pause and Resume | 5.2.9 |
| 5.4.2 | Implement notification for human review via [[messaging/discord|Discord]] / [[messaging/telegram|Telegram]] (with accumulated context summary) | 5.4.1 |
| 5.4.3 | Implement `human.review.completed` event handling: resume run from saved conversation state per [[playbooks#9. Human-in-the-Loop]] | 5.4.1 |
| 5.4.4 | Implement timeout for paused runs (configurable, default 24h, transition to timeout node or fail) per [[playbooks#9. Human-in-the-Loop]] Timeout | 5.4.1 |
| 5.4.5 | Implement `resume_playbook` command per [[playbooks#15. Playbook Commands]] | 5.4.3 |
| 5.4.6 | Add tests: playbook pauses at human node, resumes correctly after human input | 5.4.3 |
| 5.4.7 | Add tests: paused playbook times out and fails/transitions correctly | 5.4.4 |

### 5.5 Playbook Commands

**Spec:** [[playbooks#15. Playbook Commands]]

| # | Task | Depends On |
|---|---|---|
| 5.5.1 | Implement `compile_playbook` command (manual compilation trigger) | 5.1.2 |
| 5.5.2 | Implement `dry_run_playbook` command (simulate with mock event, no side effects) per [[playbooks#19. Open Questions]] #2 | 5.2.2 |
| 5.5.3 | Implement `show_playbook_graph` command (ASCII or mermaid output of compiled graph) | 5.1.4 |
| 5.5.4 | Implement `list_playbooks` command (all playbooks across scopes with status and last run) | 5.3.1 |
| 5.5.5 | Implement `list_playbook_runs` command (recent runs with status/path taken) | 5.2.9 |
| 5.5.6 | Implement `inspect_playbook_run` command (full node trace, conversation, token usage) | 5.2.9 |
| 5.5.7 | Register all playbook commands in [[specs/command-handler|CommandHandler]] via [[specs/tiered-tools|tool registry]] | 5.5.1–5.5.6 |
| 5.5.8 | Add tests: each command returns correct output for various scenarios | 5.5.7 |

### 5.6 Default Playbooks & Migration

**Spec:** [[playbooks#12. Default Playbooks]], [[playbooks#13. Migration Path]]

| # | Task | Depends On |
|---|---|---|
| 5.6.1 | Write default `task-outcome.md` playbook (consolidates post-action-reflection + spec-drift-detector + error-recovery-monitor) per [[playbooks#12. Default Playbooks]] | 5.1.2 |
| 5.6.2 | Write default `system-health-check.md` playbook (30m, replaces periodic-project-review) per [[playbooks#12. Default Playbooks]] | 5.1.2 |
| 5.6.3 | Write default `codebase-inspector.md` playbook (4h, replaces proactive-codebase-inspector rule) per [[playbooks#12. Default Playbooks]] | 5.1.2 |
| 5.6.4 | Write default `dependency-audit.md` playbook (24h, replaces dependency-update-check rule) per [[playbooks#12. Default Playbooks]] | 5.1.2 |
| 5.6.5 | Install default playbooks to vault on first run | 5.6.1–5.6.4 |
| 5.6.6 | Validate default playbooks produce equivalent results to current rules (run both, compare) per [[playbooks#13. Migration Path]] Phase 1 | 5.6.5, 5.3.1 |
| 5.6.7 | Migrate plugin `@cron()` hooks to timer-triggered playbooks per [[playbooks#16. Plugin Integration]] | 5.6.6 |
| 5.6.8 | Remove default rules when playbook equivalents are validated per [[playbooks#13. Migration Path]] Phase 2 | 5.6.6 |

### 5.7 Observability (Future)

**Spec:** [[playbooks#14. Dashboard Visualization (Future)]], [[playbooks#19. Open Questions]] #4

| # | Task | Depends On |
|---|---|---|
| 5.7.1 | Implement playbook health metrics: tokens per node, run duration, transition paths, failure rates per [[playbooks#19. Open Questions]] #4 | 5.2.9 |
| 5.7.2 | Design dashboard playbook graph view (nodes as boxes, transitions as arrows, live state highlighting) per [[playbooks#14. Dashboard Visualization (Future)]] | 5.7.1 |

> **Test checkpoint:** Full system test with playbooks running alongside hooks.
> Trigger `task.completed` — verify both the old hook AND the new playbook fire.
> Compare outputs. Verify token budgets tracked correctly. Test a human-in-the-loop
> playbook end-to-end via Discord/Telegram. Verify timer-based playbooks fire on
> schedule. Test composition: playbook A completes → playbook B triggers via filtered
> event. This is the biggest validation point in the entire roadmap.

---

## Phase 6: Self-Improvement Loop

Close the loop: agents learn from experience.

**Source:** [[self-improvement]]

### 6.1 Reflection Playbooks

**Spec:** [[self-improvement#2. The Loop]], [[memory-scoping#10. Reflection Playbook (Periodic Consolidation)]]

| # | Task | Depends On |
|---|---|---|
| 6.1.1 | Write coding agent reflection playbook (`vault/agent-types/coding/playbooks/reflection.md`) per [[self-improvement#2. The Loop]] | 5.1.2 |
| 6.1.2 | Write generic agent reflection playbook template for other agent types | 6.1.1 |
| 6.1.3 | Implement reflection playbook trigger on `task.completed` for matching agent type per [[playbooks#8. Scoping]] agent-type scope | 5.3.3, 6.1.1 |
| 6.1.4 | Implement memory consolidation within reflection: merge duplicates, update outdated, promote cross-scope per [[memory-scoping#10. Reflection Playbook (Periodic Consolidation)]] | 6.1.3, 3.4.1 |
| 6.1.5 | Verify reflection playbook reads task records, extracts patterns, writes insights to agent-type memory | 6.1.3 |

### 6.2 Log Analysis

**Spec:** [[self-improvement#2. The Loop]] — Log Analysis Playbook

| # | Task | Depends On |
|---|---|---|
| 6.2.1 | Write log analysis playbook (`vault/system/playbooks/log-analysis.md`) per [[self-improvement#2. The Loop]] | 5.1.2 |
| 6.2.2 | Implement log access tools for playbook use (read recent logs, filter by severity/date) | 6.2.1 |
| 6.2.3 | Verify log analysis writes operational insights to orchestrator memory per [[self-improvement#5. Orchestrator Memory]] | 6.2.1 |

### 6.3 Reference Stub Indexer

**Spec:** [[vault#4. Reference Stubs for External Docs]]

| # | Task | Depends On |
|---|---|---|
| 6.3.1 | Implement workspace spec/doc change detector (file watcher or git diff based) per [[vault#4. Reference Stubs for External Docs]] Generation | 1.3.1 |
| 6.3.2 | Implement stub generator: read full doc, LLM-summarize, write to `vault/projects/{id}/references/` per [[vault#4. Reference Stubs for External Docs]] Stub Format | 6.3.1 |
| 6.3.3 | Implement source_hash tracking to avoid regenerating unchanged stubs | 6.3.2 |
| 6.3.4 | Implement stale stub detection: flag stubs where `source_hash` no longer matches source file per [[vault#7. Open Questions]] #2 | 6.3.3 |
| 6.3.5 | Add tests: spec file changes → stub regenerated with updated summary | 6.3.2 |
| 6.3.6 | Add tests: unchanged spec → stub not regenerated | 6.3.3 |

### 6.4 Orchestrator Memory

**Spec:** [[self-improvement#5. Orchestrator Memory]]

| # | Task | Depends On |
|---|---|---|
| 6.4.1 | Implement startup scan of `vault/projects/*/README.md` → generate orchestrator summaries in `vault/orchestrator/memory/project-{id}.md` per [[self-improvement#5. Orchestrator Memory]] | 1.1.1, 2.2.5 |
| 6.4.2 | Wire README file watcher from Phase 1.3 to orchestrator re-summary per [[self-improvement#5. Orchestrator Memory]] On README change | 1.3.5, 6.4.1 |
| 6.4.3 | Add tests: new project README → orchestrator summary created. README change → summary updated | 6.4.2 |

### 6.5 Memory Health

**Spec:** [[self-improvement#6. Memory Health & Observability]]

| # | Task | Depends On |
|---|---|---|
| 6.5.1 | Implement memory audit trail in frontmatter (created, source_task, source_playbook, last_retrieved, retrieval_count) per [[self-improvement#6. Memory Health & Observability]] Memory Audit Trail | 2.1.13 |
| 6.5.2 | Implement `memory_health` command: collection sizes, growth rate, stale count, most-retrieved, retrieval hit rate, contradictions per [[self-improvement#6. Memory Health & Observability]] Memory Health View | 6.5.1 |
| 6.5.3 | Implement stale memory detection (not retrieved in N days) per [[self-improvement#6. Memory Health & Observability]] | 6.5.1 |
| 6.5.4 | Implement contradiction detection: flag memories tagged `#contested` per [[self-improvement#7. Open Questions]] #2 | 6.5.1 |
| 6.5.5 | Add stale memory flagging and contradiction surfacing to reflection playbook | 6.5.3, 6.5.4, 6.1.1 |

> **Test checkpoint:** Let the system run for a day with reflection playbooks active.
> Check: did insights get extracted from completed tasks? Did duplicate insights get
> merged? Are retrieval counts incrementing? Are stale memories flagged? Is the
> orchestrator's project understanding current? Are contradictions detected and
> surfaced? This validates the entire [[self-improvement|self-improvement loop]].

---

## Phase 7: Agent Coordination

Playbook-driven multi-agent workflows.

**Source:** [[agent-coordination]]

### 7.1 Workflow Infrastructure

**Spec:** [[agent-coordination#6. Workflow Runtime]]

| # | Task | Depends On |
|---|---|---|
| 7.1.1 | Add `workflow_id` nullable field to tasks table (Alembic migration) per [[agent-coordination#6. Workflow Runtime]] Workflow State | — |
| 7.1.2 | Create `workflows` DB table (Alembic migration) with fields per [[agent-coordination#6. Workflow Runtime]] Workflow State | — |
| 7.1.3 | Implement Workflow CRUD queries (create, update status, add task, get by ID) | 7.1.2 |
| 7.1.4 | Implement `workflow.stage.completed` event emission per [[agent-coordination#6. Workflow Runtime]] | 7.1.3 |
| 7.1.5 | Add tests: workflow creation, task association, status transitions | 7.1.3 |

### 7.2 Coordination Commands

**Spec:** [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] — The Interface Between Them

| # | Task | Depends On |
|---|---|---|
| 7.2.1 | Extend `create_task` command with `agent_type`, `affinity_agent_id`, `workspace_mode` parameters per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] | 7.1.1 |
| 7.2.2 | Implement `set_project_constraint` command (exclusive access, max agents by type, pause scheduling) per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] | — |
| 7.2.3 | Implement `release_project_constraint` command | 7.2.2 |
| 7.2.4 | Implement constraint enforcement in scheduler (check before assignment) per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] What the Scheduler Owns | 7.2.2 |
| 7.2.5 | Add tests: constraint blocks assignment, release allows it | 7.2.4 |

### 7.3 Agent Affinity

**Spec:** [[agent-coordination#3. Core Concepts]] Agent Affinity, [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation

| # | Task | Depends On |
|---|---|---|
| 7.3.1 | Add `affinity_agent_id` and `affinity_reason` fields to tasks table per [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation | 7.2.1 |
| 7.3.2 | Implement scheduler affinity logic: prefer idle affinity agent, bounded wait up to N seconds, fallback per [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation | 7.3.1 |
| 7.3.3 | Implement agent type matching: task's `agent_type` field matched against agent's type during assignment per [[agent-coordination#3. Core Concepts]] Agent Affinity | 7.3.1 |
| 7.3.4 | Add tests: affinity agent preferred when idle, fallback when busy too long | 7.3.2 |
| 7.3.5 | Add tests: agent type mismatch prevents assignment | 7.3.3 |

### 7.4 Workspace Modes

**Spec:** [[agent-coordination#7. Workspace Strategy]]

| # | Task | Depends On |
|---|---|---|
| 7.4.1 | Add `lock_mode` field to workspace acquisition (default: `exclusive`) per [[agent-coordination#7. Workspace Strategy]] Lock Modes | — |
| 7.4.2 | Implement `branch-isolated` lock mode (multiple agents, same repo, different branches) per [[agent-coordination#7. Workspace Strategy]] | 7.4.1 |
| 7.4.3 | Implement git mutex for shared operations (fetch, gc) in branch-isolated mode per [[agent-coordination#7. Workspace Strategy]] | 7.4.2 |
| 7.4.4 | Add tests: two agents with branch-isolated mode work concurrently on separate branches | 7.4.2 |
| 7.4.5 | Add tests: exclusive mode still prevents concurrent access | 7.4.1 |
| 7.4.6 | Stub `directory-isolated` mode as future placeholder per [[agent-coordination#7. Workspace Strategy]] (deferred) | — |

### 7.5 Coordination Playbooks

**Spec:** [[agent-coordination#4. Coordination Playbook Examples]], [[agent-coordination#9. Default Coordination Playbooks]]

| # | Task | Depends On |
|---|---|---|
| 7.5.1 | Write `feature-pipeline.md` default coordination playbook per [[agent-coordination#4. Coordination Playbook Examples]] Example 1 | 5.1.2, 7.1.3 |
| 7.5.2 | Write `bugfix-pipeline.md` default coordination playbook per [[agent-coordination#9. Default Coordination Playbooks]] | 5.1.2, 7.1.3 |
| 7.5.3 | Write `review-cycle.md` default coordination playbook per [[agent-coordination#9. Default Coordination Playbooks]] | 5.1.2, 7.1.3 |
| 7.5.4 | Write `exploration.md` default coordination playbook per [[agent-coordination#4. Coordination Playbook Examples]] Example 2 | 5.1.2, 7.1.3 |
| 7.5.5 | Implement long-running playbook support (event-triggered resumption across workflow stages) per [[agent-coordination#6. Workflow Runtime]] Workflow ↔ PlaybookRun Relationship | 5.4.1, 7.1.4 |
| 7.5.6 | Implement orphan workflow recovery: if coordination playbook crashes, tasks continue, playbook can be re-triggered per [[agent-coordination#11. Open Questions]] #2 | 7.5.5 |
| 7.5.7 | Add tests: feature pipeline creates coding → review + QA → merge task chain with correct dependencies | 7.5.1 |
| 7.5.8 | Add tests: review feedback creates fix task with affinity to original agent | 7.5.1, 7.3.2 |
| 7.5.9 | Add tests: exploration creates parallel tasks (no inter-dependency), reviewer depends on all | 7.5.4 |
| 7.5.10 | Add tests: orphan recovery — kill playbook mid-workflow, verify tasks continue, new playbook picks up | 7.5.6 |

### 7.6 Coordination Observability (Future)

**Spec:** [[agent-coordination#11. Open Questions]] #6

| # | Task | Depends On |
|---|---|---|
| 7.6.1 | Design workflow pipeline view for dashboard (stages with tasks, agent assignments, progress) per [[agent-coordination#11. Open Questions]] #6 | 7.5.5 |

> **Test checkpoint:** End-to-end coordination test: create a FEATURE task. Verify
> the feature-pipeline playbook triggers, creates a coding task with agent affinity,
> waits for PR via event, creates review + QA tasks (running concurrently via
> [[specs/scheduler-and-budget|scheduler]] dependency DAG), handles review feedback
> cycle with affinity back to original agent, and completes the workflow. Verify the
> scheduler respects agent type matching and the dependency graph drives concurrency
> per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]].

---

## Phase 8: Hook Engine Deprecation

Remove the old system once [[playbooks]] are validated.

**Source:** [[playbooks#13. Migration Path]] — Phase 3

| # | Task | Depends On |
|---|---|---|
| 8.1 | Migrate all user-created active rules to playbooks (generate playbook markdown from rule files) per [[playbooks#13. Migration Path]] Phase 2 | 5.6.6 |
| 8.2 | Migrate passive rules to vault memory files (in appropriate agent-type or project scope) per [[playbooks#13. Migration Path]] Passive Rules | 3.1.1 |
| 8.3 | Redirect hook commands to playbook equivalents (`list_hooks` → `list_playbooks`, etc.) per [[playbooks#13. Migration Path]] Phase 3 | 5.5.7 |
| 8.4 | Remove `HookEngine` ([[specs/hooks]]), `RuleManager` ([[specs/rule-system]]), and related code | 8.1, 8.2, 8.3 |
| 8.5 | Remove hook/rule DB tables (Alembic migration) | 8.4 |
| 8.6 | Remove `src/memory.py` MemoryManager and v1 memory plugin per [[memory-plugin#2. Current Architecture (Being Replaced)]] | 2.2.16 |
| 8.7 | Update all specs to remove "future evolution" callouts (they're now current) | 8.4 |
| 8.8 | Remove deprecated spec files (`proactive-inspector.md` already removed, verify no others) | 8.7 |

> **Final test checkpoint:** Full regression test. Every feature that worked with
> hooks still works with playbooks. Memory operations all route through v2 plugin.
> No references to hook engine in active code paths. Run entire test suite.

---

## Summary

| Phase | Tasks | Key Deliverable | Source Specs | Depends On |
|---|---|---|---|---|
| **0** | 23 | Prerequisite refactors | [[playbooks]] §17 | — |
| **1** | 19 | Vault structure + file watcher | [[vault]] | — |
| **2** | 39 | memsearch fork + memory plugin v2 | [[memory-plugin]], [[memory-scoping]] §7 | Phase 1 (partial) |
| **3** | 27 | Memory scoping, tiers, overrides, dedup | [[memory-scoping]] | Phase 2 |
| **4** | 18 | Profiles as markdown | [[profiles]] | Phase 1, Phase 3 (partial) |
| **5** | 48 | Playbook system | [[playbooks]] | Phase 0, Phase 1 |
| **6** | 18 | Self-improvement loop | [[self-improvement]], [[vault]] §4 | Phase 3, Phase 5 |
| **7** | 27 | Agent coordination | [[agent-coordination]] | Phase 5 |
| **8** | 8 | Hook engine deprecation | [[playbooks]] §13 | Phase 5, Phase 6 |
| **Total** | **227** | | | |

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
