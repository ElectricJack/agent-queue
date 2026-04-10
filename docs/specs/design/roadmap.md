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

## Phase 0: Prerequisite Refactors ✅ COMPLETE

All 23 tasks completed and verified. 542 Phase 0 tests passing. 52 event schemas
defined. EventBus filtering, git event emission, Supervisor config flexibility,
and task records migration all landed on `next-gen`.

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
| 0.1.3 | Add tests: EventBus filtered subscriptions. Cases: (a) subscriber with filter `{"project_id": "foo"}` receives event with matching project_id, (b) same subscriber does NOT receive event with different project_id, (c) subscriber with multi-field filter only receives events matching ALL fields, (d) multiple filtered subscribers on same event type each receive only their matches, (e) mix of filtered and unfiltered subscribers — unfiltered gets all, filtered gets only matches, (f) filter on nested payload field works correctly, (g) filter with None/null value matches events where field is absent or null | 0.1.2 |
| 0.1.4 | Add tests: EventBus backward compatibility for unfiltered subscriptions. Cases: (a) subscriber registered without filter receives ALL events of that type (same as pre-filter behavior), (b) existing test suite passes without modification, (c) subscriber with `filter=None` behaves identically to subscriber with no filter arg, (d) subscriber with empty filter `{}` receives all events (no conditions to fail), (e) unfiltered subscriber still receives events that also match another subscriber's filter | 0.1.2 |
| 0.1.5 | Add tests: EventBus filter behavior on missing/extra payload fields. Cases: (a) event payload missing a field required by filter is NOT delivered to that subscriber, (b) event with extra fields beyond filter still matches (filter is subset check, not exact), (c) event with empty payload `{}` does not match any filter with required fields, (d) filter on field that is present but with wrong type (e.g., filter expects string, payload has int) does not match, (e) rapid emission of mixed matching/non-matching events delivers correct subset in order | 0.1.2 |

> **Test checkpoint:** Run full existing event-bus test suite + new filter tests.
> Verify zero regressions in hook engine and orchestrator event handling.
> Specific validations:
> - All pre-existing `test_event_bus.py` tests pass without modification
> - Hook engine still fires correctly for `task.completed`, `task.failed`, etc.
> - Orchestrator event subscriptions (task lifecycle, agent status) still work
> - New filter tests cover single-field, multi-field, missing-field, and mixed scenarios
> - Performance: subscribing 100+ filtered subscribers does not degrade emit latency significantly
> - No memory leaks from filter dict references held by EventBus

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
| 0.2.6 | Add tests: Event Schema Registry validation. Cases: (a) event with all required fields passes validation silently, (b) event missing a required field triggers warning in prod mode and error in dev mode, (c) event with extra fields beyond schema passes (forward compatibility), (d) event with wrong field type (e.g., `project_id` as int instead of str) triggers validation error, (e) unregistered event type passes through without validation (graceful degradation), (f) all existing event types (`task.*`, `note.*`, `file.*`, `plugin.*`, `config.*`) have schemas and current emissions pass them, (g) validation error message includes event type, field name, and expected type for debugging | 0.2.3 |

### 0.3 GitManager Event Emission

Add event emission to existing git operations. Enables code quality gates and
commit-triggered playbooks.

**Spec:** [[playbooks#7. Event System]] — New Events Needed, [[playbooks#17. Prerequisite Refactors]] — GitManager Event Emission
**Existing code:** `src/git/manager.py` ([[specs/git]])

| #     | Task                                                                                                                     | Depends On          |
| ----- | ------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| 0.3.1 | Define schemas for `git.commit`, `git.push`, `git.pr.created` events                                                     | 0.2.1               |
| 0.3.2 | Emit `git.commit` from `GitManager.acommit_all()` with commit_hash, branch, changed_files, message, project_id, agent_id | —                   |
| 0.3.3 | Emit `git.push` from `GitManager.apush_branch()` with branch, remote, commit_range, project_id                           | —                   |
| 0.3.4 | Emit `git.pr.created` from `GitManager.create_pr()` with pr_url, branch, title, project_id                               | —                   |
| 0.3.5 | Add tests: GitManager event emission. Cases: (a) `acommit_all()` emits `git.commit` with commit_hash, branch, changed_files list, message, project_id, and agent_id — verify each field present and correct, (b) `apush_branch()` emits `git.push` with branch, remote, commit_range, project_id — verify remote defaults to "origin", (c) `create_pr()` emits `git.pr.created` with pr_url, branch, title, project_id — verify URL is valid, (d) failed git operation (e.g., push to protected branch) does NOT emit event, (e) events pass the schemas defined in 0.3.1, (f) event payloads are captured by an EventBus subscriber (integration test with real EventBus), (g) concurrent git operations from different agents emit separate events with correct agent_id isolation | 0.3.2, 0.3.3, 0.3.4 |

> **Test checkpoint:** Create a task, let an agent commit + push + create PR.
> Verify all three git events fire with correct payloads in the event log.
> Specific validations:
> - `git.commit` event contains accurate commit_hash (matches `git log` output)
> - `git.commit` changed_files list matches actual files in the commit diff
> - `git.push` commit_range accurately reflects the pushed commits
> - `git.pr.created` pr_url is a valid GitHub/GitLab URL that resolves
> - All three events carry the correct `project_id` from the task context
> - Events are emitted in correct temporal order (commit before push before PR)
> - Existing git operations (checkout, fetch, clone) do NOT emit spurious events

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
| 0.4.4 | Add tests: Supervisor model override via `llm_config`. Cases: (a) `chat()` with `llm_config={"model": "gpt-4o"}` routes to OpenAI provider instead of default, (b) `chat()` with `llm_config={"model": "claude-sonnet-4-20250514"}` routes to Anthropic provider, (c) `chat()` without `llm_config` uses the default model from agent profile (backward compat), (d) `llm_config` with invalid/unknown model returns clear error, does not fall back silently, (e) model override applies only to that single call — subsequent calls without override use default, (f) `llm_config` with additional parameters (temperature, max_tokens) are passed through to provider | 0.4.2 |
| 0.4.5 | Add tests: Supervisor tool restriction via `tool_overrides`. Cases: (a) `chat()` with `tool_overrides=["read_file", "write_file"]` only exposes those two tools to the LLM, (b) LLM attempt to call a tool not in the override list is blocked with clear error, (c) `chat()` without `tool_overrides` exposes the full default tool set (backward compat), (d) empty `tool_overrides=[]` disables all tools (LLM can only produce text), (e) `tool_overrides` with unknown tool name raises validation error before LLM call, (f) tool restriction applies only to that single call — next call without override has full tools | 0.4.3 |

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
| 0.5.6 | Add tests: Task records migration. Cases: (a) new task record writes to `~/.agent-queue/tasks/{project_id}/` not `memory/*/tasks/`, (b) memory search query returns zero results from task files (no task pollution), (c) migration script moves existing task files and preserves content byte-for-byte, (d) migration script is idempotent — running twice does not duplicate or corrupt files, (e) old task path is empty after migration, (f) task read operations find records at new location, (g) projects with no existing tasks do not cause migration errors (empty source dir), (h) re-index after migration produces a clean collection with no task entries | 0.5.5 |

> **Test checkpoint:** Full system test — create tasks, complete them, verify records
> appear in new location. Memory search should return cleaner results without task noise.
> Specific validations:
> - Create 5+ tasks across 2 projects, complete them — all records in `tasks/{project_id}/`
> - Memory search for project-related terms returns notes and insights, NOT task records
> - Verify task records retain all metadata (status, timestamps, agent_id, outcome)
> - Migration from old layout: seed old-style task files, run migration, verify new paths
> - Memory index file counts decrease after re-index (task files no longer indexed)
> - System startup with fresh install creates `tasks/` directory structure automatically

---

## Phase 1: Vault Structure ✅ COMPLETE

Vault directory structure created, content migration implemented, unified
VaultWatcher with path-based dispatch for all 6 handler types landed.
All tests passing.

Originally: Create the vault directory layout and the unified file watcher.

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
| 1.3.9 | Add tests: VaultWatcher path-based dispatch. Cases: (a) creating/editing a file in `*/playbooks/*.md` triggers the playbook compilation handler only, (b) creating/editing `*/profile.md` triggers the profile sync handler only, (c) creating/editing a file in `*/memory/**/*.md` triggers the memory re-index handler, (d) creating/editing `projects/*/README.md` triggers the orchestrator summary handler, (e) creating/editing a file in `*/overrides/*.md` triggers the override re-index handler, (f) creating/editing `*/facts.md` triggers the KV sync handler, (g) editing a file outside any watched path triggers NO handler, (h) deleting a watched file triggers the appropriate handler (not just create/modify), (i) renaming a file across path categories triggers both the old-path handler (delete) and new-path handler (create) | 1.3.2–1.3.7 |
| 1.3.10 | Add tests: VaultWatcher debounce behavior. Cases: (a) editing the same file 10 times in 100ms triggers the handler only once (debounce window), (b) editing two different files in the same category in quick succession triggers handler once per file (separate debounce keys), (c) editing a file, waiting past debounce window, editing again triggers handler twice, (d) debounce does not drop the final state — handler receives the latest content, (e) handler errors during debounced call do not prevent future triggers for the same file, (f) debounce window is configurable and defaults to a reasonable value (e.g., 500ms) | 1.3.8 |

> **Test checkpoint:** Create vault structure, edit files in each location, verify
> the correct handler fires. Edit a profile.md, verify change is detected. Edit
> a memory file, verify re-index triggered. This is the foundation — it must be solid.
> Specific validations:
> - Vault directory structure matches [[vault#2. Directory Layout]] exactly (all subdirs present)
> - Each of the 6 dispatch paths (playbooks, profiles, memory, README, overrides, facts) fires the correct handler and no others
> - File watcher detects changes within 1 second on all supported platforms (Linux inotify, macOS FSEvents)
> - Debounce prevents handler storms during bulk file operations (e.g., git checkout switching many files)
> - Handler exceptions are logged but do not crash the watcher or block other handlers
> - Watcher survives vault directory being temporarily unavailable (e.g., network mount disconnect)
> - Content migration from Phase 1.2 triggers appropriate handlers upon startup (or is explicitly suppressed during migration)

---

## Phase 2: memsearch Fork & Memory Plugin v2

Fork memsearch, add KV + temporal + topic support, build the new plugin.

**Source:** [[memory-plugin#3. New Architecture]], [[memory-plugin#5. memsearch Fork]],
[[memory-plugin#6. Collection Schema]], [[memory-plugin#7. Milvus Backend Topology]]

### 2.1 memsearch Fork

**Spec:** [[memory-plugin#5. memsearch Fork]], [[memory-plugin#6. Collection Schema]]

| #      | Task                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Depends On   |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| 2.1.1  | ~~Fork memsearch to internal repo~~ ✅ DONE — forked to ElectricJack/memsearch, added as git subtree at `packages/memsearch/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | —            |
| 2.1.2  | Implement unified collection schema per [[memory-plugin#6. Collection Schema]] (entry_type, content, original, kv fields, valid_from/to, topic, tags, updated_at). Work in `packages/memsearch/`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | ✅ 2.1.1     |
| 2.1.3  | Implement KV insert/query methods (scalar-only, no vector search) per [[memory-plugin#6. Collection Schema]] KV Queries                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 2.1.2        |
| 2.1.4  | Implement temporal insert/query methods (valid_from/valid_to windowed lookups) per [[memory-plugin#6. Collection Schema]] Temporal Queries                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 2.1.2        |
| 2.1.5  | Implement temporal fact lifecycle (close old window, open new on update) per [[memory-plugin#6. Collection Schema]] Temporal Fact Lifecycle                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 2.1.4        |
| 2.1.6  | Implement historical "as-of" query method                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 2.1.4        |
| 2.1.7  | Implement topic field support (scalar filter before vector search) per [[memory-scoping#3. Topic Filtering]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 2.1.2        |
| 2.1.8  | Implement topic filter fallback: auto-widen to unfiltered when < 3 results per [[memory-scoping#3. Topic Filtering]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 2.1.7        |
| 2.1.9  | Implement multi-collection parallel search with weighted merging per [[memory-scoping#4. Scope Hierarchy]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 2.1.2        |
| 2.1.10 | Implement scope-aware collection naming (`aq_system`, `aq_agenttype_*`, `aq_project_*`) per [[memory-plugin#7. Milvus Backend Topology]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 2.1.2        |
| 2.1.11 | Implement tag-based cross-collection search per [[memory-plugin#7. Milvus Backend Topology]] Tag-Based Cross-Scope Discovery                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 2.1.2        |
| 2.1.12 | Implement `original` field storage (full content alongside summary embedding) per [[memory-scoping#9. Summary + Original Pattern]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | 2.1.2        |
| 2.1.13 | Implement retrieval tracking (update `retrieval_count`, `last_retrieved`) per [[self-improvement#6. Memory Health & Observability]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | 2.1.2        |
| 2.1.14 | Implement embedding model version tracking per collection for future re-indexing per [[memory-plugin#8. Open Questions]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 2.1.2        |
| 2.1.15 | Add tests: KV insert/query round-trip per [[memory-plugin#6. Collection Schema]] KV Queries. Cases: (a) insert a KV pair and query by exact key returns correct value, (b) insert multiple KV pairs with different keys and query each independently, (c) overwrite an existing key and verify query returns the new value, (d) query for non-existent key returns empty/None (not error), (e) KV query uses scalar-only path (no vector search invoked — verify with mock), (f) insert KV pair with namespace and query with same namespace returns it, query with different namespace does not, (g) KV values can store complex strings (multi-line, unicode, special characters)                                                                                                                        | 2.1.3        |
| 2.1.16 | Add tests: Temporal fact lifecycle per [[memory-plugin#6. Collection Schema]] Temporal Fact Lifecycle. Cases: (a) insert temporal fact with valid_from=now, valid_to=None — as-of query at now returns it, (b) update fact: old record gets valid_to=now, new record gets valid_from=now — as-of query at now returns new value, (c) as-of query at past timestamp returns the old value (before update), (d) as-of query at future timestamp returns current value (valid_to=None), (e) multiple updates create a complete history chain — as-of query at each point returns correct version, (f) temporal query with no matching time window returns empty, (g) concurrent updates to same fact do not corrupt the window chain (no overlapping valid_from/valid_to)                                     | 2.1.5, 2.1.6 |
| 2.1.17 | Add tests: Topic-filtered search per [[memory-scoping#3. Topic Filtering]]. Cases: (a) search with topic="testing" returns only memories tagged with "testing" topic, (b) search without topic filter returns memories across all topics, (c) topic filter with < 3 results auto-widens to unfiltered search and returns more results (fallback per spec), (d) topic filter with >= 3 results does NOT widen (stays filtered), (e) search with non-existent topic returns 0 filtered results then falls back to unfiltered, (f) topic filter is applied as scalar pre-filter before vector similarity (verify with query plan or mock), (g) fallback results are clearly distinguishable from direct matches (e.g., metadata flag)                                                                         | 2.1.7, 2.1.8 |
| 2.1.18 | Add tests: Multi-collection weighted merge per [[memory-scoping#4. Scope Hierarchy]]. Cases: (a) search across 3 collections with weights [1.0, 0.7, 0.4] — result from weight-1.0 collection ranks above equally-similar result from weight-0.4 collection, (b) very high similarity in low-weight collection can still outrank moderate similarity in high-weight collection (weight adjusts score, doesn't override), (c) empty collection in the merge set does not cause errors, (d) results are deduplicated across collections (same content in two scopes appears once with highest weighted score), (e) merge respects requested result limit (top-K after merge, not top-K per collection), (f) parallel search across collections completes within reasonable time (not sequential N * latency) | 2.1.9        |
| 2.1.19 | Add tests: Cross-collection tag search per [[memory-plugin#7. Milvus Backend Topology]] Tag-Based Cross-Scope Discovery. Cases: (a) memory tagged `#api-pattern` in project collection is found by tag search from system scope, (b) tag search returns results from multiple collections with correct source attribution, (c) tag search with non-existent tag returns empty (not error), (d) memory with multiple tags is found by search on any single tag, (e) tag search combined with topic filter narrows results correctly, (f) tag names are case-insensitive (`#API` matches `#api`), (g) special characters in tags (hyphens, underscores) work correctly                                                                                                                                       | 2.1.11       |
| 2.1.20 | Add tests: Retrieval tracking per [[self-improvement#6. Memory Health & Observability]]. Cases: (a) searching and returning a memory increments its `retrieval_count` by 1, (b) `last_retrieved` timestamp updates to current time on retrieval, (c) memory not returned by search has retrieval_count unchanged, (d) multiple searches returning same memory increment count cumulatively, (e) retrieval tracking works for both semantic search and KV query, (f) retrieval tracking does not slow down search response time noticeably (< 10% overhead), (g) initial retrieval_count is 0 and last_retrieved is null for newly inserted memories                                                                                                                                                        | 2.1.13       |

> **Test checkpoint:** Run the full memsearch fork test suite against Milvus Lite.
> Verify all new features work in isolation before integrating with the plugin.
> Specific validations:
> - Original memsearch test suite passes (no regressions in base functionality)
> - KV round-trip: insert 50 key-value pairs, query each, verify 100% accuracy
> - Temporal: create a fact, update it 5 times, query as-of each historical timestamp
> - Topic filter: insert 20 memories across 4 topics, verify filtered search returns only matching topic
> - Topic fallback: search a topic with 1 result, verify auto-widen returns additional cross-topic results
> - Multi-collection merge: create 3 collections with overlapping content, verify weighted ranking is correct
> - Tag search: tag memories in different collections, verify cross-collection discovery works
> - Retrieval tracking: search 10 times, verify counts and timestamps are accurate
> - Collection naming follows `aq_system`, `aq_agenttype_*`, `aq_project_*` convention
> - All tests run against Milvus Lite (in-process, no external dependency)

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
| 2.2.16 | Add tests: MCP memory tool round-trips per [[memory-scoping#7. Agent Memory Tools (MCP)]]. Cases: (a) `memory_save` then `memory_search` returns the saved content with high similarity, (b) `memory_store` a KV pair then `memory_recall` retrieves exact value, (c) `memory_list` returns all memories in scope with correct metadata, (d) `memory_list_facts` returns all KV entries in scope/namespace, (e) `memory_save` with duplicate content does not create a second entry (dedup), (f) `memory_search` with no results returns empty list (not error), (g) `memory_store` then overwrite same key then `memory_recall` returns latest value, (h) all tools return well-formed response dicts with `success` field | 2.2.4–2.2.11 |
| 2.2.17 | Add tests: facts.md bidirectional sync per [[memory-plugin#7. Milvus Backend Topology]] Fact Files. Cases: (a) parse a facts.md with `key: value` pairs under headings — each pair appears as KV entry in collection, (b) facts.md with multiple headings creates KV entries with heading as namespace, (c) `memory_recall` for a key parsed from facts.md returns correct value, (d) editing facts.md (change a value) triggers re-parse and updates KV entry in collection, (e) `memory_store` a new KV pair triggers facts.md writer to append the entry to the file, (f) facts.md with malformed lines (no colon, empty value) logs warning but does not crash — valid lines still parsed, (g) facts.md with markdown formatting in values (bold, links) preserves formatting in stored value | 2.2.13, 2.2.8 |
| 2.2.18 | Add tests: `memory_get` auto-routing per [[memory-scoping#7. Agent Memory Tools (MCP)]]. Cases: (a) `memory_get("preferred_language")` where a KV entry with that exact key exists returns the KV value (no vector search), (b) `memory_get("how do we handle errors in the API")` with no KV match falls through to semantic search, (c) `memory_get` with `full=true` returns original content instead of summary per [[memory-scoping#9. Summary + Original Pattern]], (d) `memory_get` for a key that exists in KV but also has semantic matches returns the KV result (KV takes priority), (e) `memory_get` with empty query returns error/empty (not crash), (f) routing decision is transparent in response metadata (indicates whether KV or semantic was used) | 2.2.11 |
| 2.2.19 | Add tests: Topic auto-detection in `memory_save` per [[memory-scoping#3. Topic Filtering]]. Cases: (a) saving content about "pytest fixtures and mocking" auto-detects topic as "testing" or similar, (b) saving content about "database schema migration" auto-detects a topic related to "database" or "infrastructure", (c) saving with explicit topic parameter overrides auto-detection, (d) saving very short content (< 10 tokens) still assigns a topic (falls back to task context), (e) topic detection is consistent — saving similar content twice assigns the same topic, (f) auto-detected topic is from a reasonable controlled vocabulary (not arbitrary free text) | 2.2.6 |

> **Test checkpoint:** End-to-end: an agent saves an insight via MCP, a second agent
> searches and finds it. An agent stores a fact, another agent recalls it. Both the
> vault markdown files and the Milvus collections are consistent.
> Specific validations:
> - Agent A calls `memory_save("API rate limiting needs exponential backoff")` — verify entry in Milvus collection
> - Agent B calls `memory_search("rate limit handling")` — verify Agent A's insight is in results
> - Agent A calls `memory_store("api_base_url", "https://api.example.com")` — verify KV in collection AND facts.md updated
> - Agent B calls `memory_recall("api_base_url")` — verify returns "https://api.example.com"
> - Edit facts.md directly (Obsidian simulation): change api_base_url value — verify `memory_recall` returns new value
> - Verify `memory_list` shows both semantic memories and KV entries with correct entry_type
> - Verify `memory_get` auto-routes correctly for both KV keys and semantic queries
> - Plugin coexists with v1 during transition: both registered, v2 handles new calls, v1 still available

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
| 3.1.8 | Add tests: Scope resolution per [[memory-scoping#4. Scope Hierarchy]]. Cases: (a) resolver for (agent_type="coding", project_id="myapp") returns collections in order: [aq_project_myapp, aq_agenttype_coding, aq_system] with weights [1.0, 0.7, 0.4], (b) resolver for (agent_type="coding", project_id=None) returns [aq_agenttype_coding, aq_system] (no project scope), (c) resolver for (agent_type=None, project_id="myapp") returns [aq_project_myapp, aq_system] (no agent-type scope), (d) resolver for unknown agent_type still returns system collection, (e) collections are created on-demand if they don't exist yet, (f) weight values match the spec exactly and are configurable | 3.1.1 |
| 3.1.9 | Add tests: KV scope resolution with first-match-wins per [[memory-scoping#6. Multi-Scope Query]]. Cases: (a) KV key exists in project scope — returns project value, does NOT query agent-type or system, (b) KV key missing from project scope, exists in agent-type scope — returns agent-type value, (c) KV key missing from project and agent-type, exists in system — returns system value, (d) KV key missing from all scopes — returns None/empty, (e) same key exists in both project and system scope — project value wins (first-match), (f) writing a KV entry writes to the most specific scope (project if project_id is set), (g) deleting a project-scope KV entry causes fallthrough to agent-type/system value | 3.1.6 |
| 3.1.10 | Add tests: Semantic search weighted merge across scopes per [[memory-scoping#6. Multi-Scope Query]]. Cases: (a) insert similar content in project (weight 1.0) and system (weight 0.4) — project result ranks first, (b) insert highly relevant content in system scope and weakly relevant in project — system result can still rank high if raw similarity is much higher, (c) search across 3 scopes with 5 results each — merged output is top-K by weighted score, (d) scope with no matching results contributes nothing to merge (no padding with low-score results), (e) results include source scope metadata so caller knows which scope each result came from, (f) total search latency is bounded (parallel scope queries, not sequential) | 3.1.7 |

### 3.2 Override Model

**Spec:** [[memory-scoping#5. Override Model]]

| # | Task | Depends On |
|---|---|---|
| 3.2.1 | Implement override file indexing (`vault/projects/{id}/overrides/{type}.md` into project collection with highest weight) per [[memory-scoping#5. Override Model]] | 3.1.1 |
| 3.2.2 | Wire override file watcher handler from Phase 1.3 | 1.3.6, 3.2.1 |
| 3.2.3 | Implement override injection into agent context alongside base [[profiles|profile]] | 3.2.1 |
| 3.2.4 | Add tests: Override indexing and retrieval per [[memory-scoping#5. Override Model]]. Cases: (a) create `vault/projects/myapp/overrides/coding.md` — content appears in project-scope search results, (b) override content has highest weight (above normal project memories) so it ranks first for matching queries, (c) override content is injected into agent context alongside base profile, (d) updating override file triggers re-index and new content appears in subsequent searches, (e) deleting override file removes it from search results, (f) override with empty content does not inject empty string into context | 3.2.1 |
| 3.2.5 | Add tests: Override scope isolation. Cases: (a) override file `overrides/coding.md` does NOT appear in searches for agent-type "qa", (b) override file `overrides/coding.md` DOES appear for agent-type "coding" in that project, (c) system-level override in `vault/system/overrides/` applies to all agent types, (d) project override takes precedence over system override for the same agent type, (e) agent with no matching override file still works normally (no override is fine), (f) override for project A does not leak into project B searches even for the same agent type | 3.2.1 |

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
| 3.3.7 | Add tests: L0+L1 tier injection per [[memory-scoping#2. Memory Tiers (L0–L3)]]. Cases: (a) every task context includes the `## Role` section from the agent's profile.md (L0, ~50 tokens), (b) every task context includes project + agent-type facts.md KV entries (L1, ~200 tokens), (c) combined L0+L1 is approximately 250 tokens baseline (verify within tolerance), (d) L0 is absent if agent has no profile.md (graceful degradation), (e) L1 is absent if no facts.md exists for the scope (no error), (f) L0+L1 content appears in the system prompt section (not user message), (g) agent with profile but no project still gets L0 + agent-type L1 facts | 3.3.5 |
| 3.3.8 | Add tests: L2 topic-filtered memory loading per [[memory-scoping#2. Memory Tiers (L0–L3)]]. Cases: (a) task about "testing the payment API" detects topic "testing"/"payments" and loads relevant topic memories (~500 tokens), (b) task about "update README" does NOT load "testing" topic memories (topic mismatch), (c) L2 memories are loaded on-demand when topic emerges mid-task (not at initial context build), (d) L2 memories do not exceed ~500 token budget (truncated or top-K limited), (e) task with no detectable topic does not load any L2 memories (L0+L1 only), (f) L2 topic detection works from both task description and ongoing conversation context | 3.3.6 |
| 3.3.9 | Add tests: L3 on-demand search via `memory_search` tool per [[memory-scoping#2. Memory Tiers (L0–L3)]]. Cases: (a) agent explicitly calls `memory_search("database optimization")` and gets results from all topics (not limited to current topic), (b) L3 search returns results from all scopes (project + agent-type + system) with correct weighted merge, (c) L3 search does not duplicate results already loaded in L1 or L2, (d) L3 search works even when L2 is not active (no topic detected), (e) L3 results include source scope and topic metadata, (f) L3 search respects the same retrieval tracking (retrieval_count increments) | 3.3.6 |

### 3.4 Deduplication & Summary

**Spec:** [[memory-scoping#8. memory_save Flow]], [[memory-scoping#9. Summary + Original Pattern]]

| #     | Task                                                                                                                                                       | Depends On    |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| 3.4.1 | Implement similarity-based dedup in `memory_save` (>0.95 timestamp update, 0.8-0.95 LLM merge, <0.8 create new) per [[memory-scoping#8. memory_save Flow]] | 2.2.5         |
| 3.4.2 | Implement LLM merge for 0.8-0.95 similarity: combine content, prefer newer on contradiction, preserve tags per [[memory-scoping#8. memory_save Flow]]      | 3.4.1         |
| 3.4.3 | Implement summary generation for long memories (>200 tokens → summarize for embedding, keep original) per [[memory-scoping#9. Summary + Original Pattern]] | 2.1.12, 2.2.5 |
| 3.4.4 | Add tests: Duplicate detection (>0.95 similarity) per [[memory-scoping#8. memory_save Flow]]. Cases: (a) saving identical content twice results in only one entry (second save updates timestamp only), (b) saving near-identical content (e.g., minor typo fix) with >0.95 similarity also deduplicates, (c) collection entry count does not increase on duplicate save, (d) the updated timestamp reflects the second save time, (e) duplicate detection works across the same scope only (same content in different scopes creates separate entries), (f) dedup check does not trigger on very short content where similarity is unreliable (< 5 tokens) | 3.4.1         |
| 3.4.5 | Add tests: LLM merge for similar content (0.8-0.95 similarity) per [[memory-scoping#8. memory_save Flow]]. Cases: (a) saving content with 0.85 similarity to existing triggers LLM merge call, (b) merged result contains information from both the old and new content, (c) on contradiction between old and new, merged result prefers newer information, (d) tags from both old and new content are preserved in merged entry, (e) merged entry replaces the old one (not two entries), (f) if LLM merge fails (provider error), original is kept and new content is saved separately with a warning, (g) merge produces content that is coherent and not just concatenated | 3.4.2         |
| 3.4.6 | Add tests: Distinct content save (<0.8 similarity) per [[memory-scoping#8. memory_save Flow]]. Cases: (a) saving content with <0.8 similarity to any existing entry creates a new entry, (b) collection entry count increases by 1 after distinct save, (c) both old and new entries are independently searchable, (d) saving to an empty collection always creates new (no dedup check needed), (e) saving 10 distinct pieces of content creates 10 entries with correct topics and tags, (f) distinct save assigns its own topic and tags independent of existing entries | 3.4.1         |
| 3.4.7 | Add tests: Summary + original pattern per [[memory-scoping#9. Summary + Original Pattern]]. Cases: (a) saving content >200 tokens generates a summary for the embedding and stores the original separately, (b) `memory_search` returns the summary (shorter, optimized for search), (c) `memory_get` with `full=true` returns the original full content, (d) `memory_get` without `full=true` returns the summary, (e) saving content <=200 tokens stores it as-is (no summary generated), (f) summary is meaningfully shorter than original (not just truncated), (g) original content is byte-for-byte identical to what was saved (no transformation loss) | 3.4.3, 2.2.12 |

> **Test checkpoint:** Full integration test: create an agent with a [[profiles|profile]],
> set up project facts, create [[memory-scoping#5. Override Model|overrides]]. Start a
> task — verify L0 (role) and L1 (facts) are in the context. Save several insights with
> topics. Start another task on the same topic — verify L2 topic memories appear. Search
> across topics — verify L3 returns cross-topic results with correct scope weighting.
> Specific validations:
> - Create coding agent with profile.md containing `## Role`, set up project facts.md with 5 KV pairs
> - Create project override `overrides/coding.md` with project-specific instructions
> - Start a task: verify context contains role (L0, ~50 tokens), facts (L1, ~200 tokens), override (highest weight)
> - Agent saves 3 insights about "testing" topic and 3 about "deployment" topic via `memory_save`
> - Start new task about testing: verify L2 loads testing-related memories, NOT deployment ones
> - Call `memory_search("deployment strategies")`: verify L3 returns deployment memories from any topic
> - Save near-duplicate insight: verify dedup merges (0.8-0.95) or updates timestamp (>0.95)
> - Save long insight (>200 tokens): verify summary generated, `full=true` returns original
> - KV scope test: store key in project, same key in system — `memory_recall` returns project value
> - Override isolation: coding agent sees override, qa agent does not
> - Scope weights: project result (1.0) outranks equally-relevant system result (0.4)

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
| 4.1.10 | Add tests: Profile parser and DB sync per [[profiles#2. Hybrid Format]], [[profiles#3. Sync Model]]. Cases: (a) valid profile.md with all sections (Config, Tools, MCP Servers, Role, Rules, Reflection) parses every field correctly, (b) parsed Config JSON values (model, permission_mode, max_tokens_per_task) sync to `agent_profiles` DB table, (c) parsed Tools list syncs and each tool name is validated against tool registry, (d) English sections (Role, Rules, Reflection) are stored as raw markdown strings, (e) profile.md with only some sections (e.g., Role + Config, no Tools) parses the present sections and leaves others as defaults, (f) round-trip: write profile → parse → sync to DB → read DB → verify all fields match, (g) sync is an upsert — existing profile is updated, not duplicated | 4.1.6 |
| 4.1.11 | Add tests: Profile error handling per [[profiles#3. Sync Model]]. Cases: (a) malformed JSON in `## Config` block triggers sync failure and sends notification, (b) DB row retains previous valid values after failed sync (no partial update), (c) invalid tool name in `## Tools` produces warning but does not block sync of other sections, (d) malformed JSON in `## MCP Servers` triggers failure notification, (e) completely empty profile.md does not crash parser (returns empty/default), (f) profile.md with valid frontmatter but garbled body sections fails gracefully, (g) error notification includes the file path and specific parse error for debugging | 4.1.8 |
| 4.1.12 | Add tests: File watcher profile sync integration. Cases: (a) edit profile.md Role section — DB updates with new Role text within one watcher cycle, (b) edit profile.md Config JSON (change model) — DB reflects new model value, (c) rapid edits to profile.md (3 edits in 500ms) trigger only one sync due to debounce, (d) creating a new profile.md in vault triggers initial sync and DB row creation, (e) deleting profile.md does NOT delete DB row (preserves last known config with warning), (f) concurrent edits to two different agents' profile.md files sync independently and correctly | 4.1.7 |

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
| 4.3.5 | Add tests: Starter knowledge pack provisioning per [[profiles#4. Starter Knowledge Packs]]. Cases: (a) creating first profile.md for agent-type "coding" copies coding knowledge pack files to agent-type memory directory, (b) copied files are tagged `#starter` in frontmatter, (c) creating second profile.md for same agent-type does NOT copy pack again (already provisioned), (d) agent-type with no matching knowledge pack creates profile without error (pack is optional), (e) knowledge pack files are indexed into agent-type memory collection after copy, (f) `#starter` tag allows users to identify and optionally remove starter content, (g) starter files are copies (not symlinks) — editing them does not affect the template | 4.3.4 |

> **Test checkpoint:** Create a new agent profile via chat command. Verify: markdown
> file appears in vault, DB row syncs, starter knowledge pack copied. Edit the
> profile.md in Obsidian — verify DB updates. Intentionally break JSON in profile —
> verify graceful failure with notification.
> Specific validations:
> - Chat command `create_profile coding-agent` creates `vault/agent-types/coding/profile.md` with template structure
> - DB `agent_profiles` row matches all parsed fields from the new profile.md
> - Starter knowledge pack for "coding" type is copied to `vault/agent-types/coding/memory/` with `#starter` tags
> - Edit profile.md `## Role` in Obsidian → DB `role` field updates within watcher cycle
> - Edit profile.md `## Config` JSON to change model → DB `model` field updates
> - Break `## Config` JSON (missing closing brace) → notification sent, DB retains previous model value
> - Fix the JSON → next watcher cycle syncs successfully, DB now has corrected value
> - Migration test: existing DB-only profile → markdown generated → DB still matches
> - Verify profile commands (list, show, update) all work with the new markdown-backed flow

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
| 5.1.8 | Add tests: Playbook compilation happy path per [[playbooks#4. Authoring Model]], [[playbooks#5. Compiled Format (JSON Schema)]]. Cases: (a) sample 3-node playbook markdown compiles to JSON that validates against the schema, (b) compiled JSON contains correct entry_node, all node definitions, and all transitions, (c) node fields (prompt, tools, llm_config, summarize_before) are correctly extracted, (d) transition fields (condition, target, structured expression) are correctly extracted, (e) frontmatter fields (trigger, scope, cooldown) are preserved in compiled output, (f) compilation is idempotent — compiling same markdown twice produces identical JSON, (g) compiled JSON is stored at correct path in `~/.agent-queue/compiled/` mirroring source scope | 5.1.2 |
| 5.1.9 | Add tests: Playbook compilation error handling per [[playbooks#4. Authoring Model]]. Cases: (a) markdown with no recognizable node structure produces compilation error notification, (b) previous valid compiled JSON is retained on disk after failed recompilation, (c) PlaybookManager continues to use the previous version for event matching, (d) error notification includes the file path and LLM/validation error details, (e) markdown with valid structure but LLM provider failure retains previous version and notifies, (f) partially valid markdown (some nodes valid, some broken) fails entire compilation (atomic — no partial updates), (g) fixing the markdown and saving again triggers successful recompilation | 5.1.7 |
| 5.1.10 | Add tests: Playbook source_hash change detection per [[playbooks#4. Authoring Model]]. Cases: (a) saving playbook markdown without content changes does NOT trigger recompilation (hash unchanged), (b) changing a comment or whitespace-only change does NOT trigger recompilation (hash based on normalized content), (c) changing a node prompt DOES trigger recompilation (hash changes), (d) after recompilation, stored source_hash matches new content, (e) compiled JSON timestamp updates only on actual recompilation, (f) force-compile command bypasses hash check and recompiles regardless | 5.1.5 |
| 5.1.11 | Add tests: Graph validation per [[playbooks#19. Open Questions]] #6. Cases: (a) graph with unreachable node (no incoming transitions, not entry) produces validation error naming the unreachable node, (b) graph with no entry node defined produces validation error, (c) transition referencing non-existent target node produces validation error with the invalid target name, (d) graph with cycle but no exit condition produces validation warning (cycles are allowed but must have exit), (e) graph with cycle AND exit condition passes validation, (f) valid graph (all nodes reachable, entry exists, all targets valid) passes validation silently, (g) graph with single node (entry = terminal) is valid, (h) graph with duplicate node names produces validation error | 5.1.3 |

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
| 5.2.13 | Add tests: Playbook execution happy path per [[playbooks#6. Execution Model]]. Cases: (a) 3-node linear playbook (start → middle → end) executes all nodes in order and completes with status "completed", (b) each node receives accumulated conversation history from prior nodes, (c) each node's prompt is built with correct context (task data, memory tier content), (d) `Supervisor.chat()` is invoked once per node with correct parameters, (e) run duration and per-node token usage are recorded, (f) final PlaybookRun status is "completed" with correct node trace [start, middle, end], (g) playbook with single node (entry = terminal) executes and completes | 5.2.3 |
| 5.2.14 | Add tests: Branching transition evaluation per [[playbooks#6. Execution Model]] Transition Evaluation. Cases: (a) node with two conditional transitions — LLM evaluator picks the correct branch based on prior node output, (b) node with three branches — middle branch is selected when conditions match, (c) transition with "else/default" condition is selected when no other conditions match, (d) transition evaluation uses the cheaper model specified in playbook config (not the node's model), (e) transition evaluation prompt includes the condition list and conversation context, (f) ambiguous conditions (multiple could match) — first matching transition wins (ordered evaluation), (g) no matching transition and no default produces run failure with descriptive error | 5.2.4 |
| 5.2.15 | Add tests: Structured transition evaluation per [[playbooks#6. Execution Model]] Transition Evaluation. Cases: (a) structured expression `task.status == "completed"` evaluates to true/false without any LLM call (verify mock not invoked), (b) structured expression referencing node output field (e.g., `output.approval == "yes"`) evaluates correctly, (c) invalid expression syntax produces clear error (not silent failure), (d) expression referencing undefined variable fails gracefully with descriptive error, (e) structured transitions are significantly faster than LLM-evaluated transitions (no network call), (f) mix of structured and LLM transitions on same node — structured is evaluated first, LLM only if structured doesn't match | 5.2.5 |
| 5.2.16 | Add tests: Token budget enforcement per [[playbooks#6. Execution Model]] Token Budget. Cases: (a) run that exceeds per-playbook token budget stops execution at current node with status "failed" and reason "token_budget_exceeded", (b) conversation history and node trace are preserved in the failed run record, (c) run approaching budget (within 10%) logs a warning but continues, (d) global daily token cap (`max_daily_playbook_tokens`) blocks new runs when exceeded, (e) daily cap resets at midnight (or configured time), (f) token counting includes both input and output tokens for each node, (g) run that would exceed budget on the FIRST node fails immediately (does not start) | 5.2.7 |
| 5.2.17 | Add tests: PlaybookRun persistence per [[playbooks#6. Execution Model]] Run Persistence. Cases: (a) completed run has DB record with status "completed", full node trace, and total token usage, (b) node trace contains ordered list of node IDs visited (e.g., ["start", "analyze", "report"]), (c) conversation history in DB matches the actual messages exchanged at each node, (d) failed run has status "failed" with error details and partial node trace up to failure point, (e) timed-out run has status "timed_out" with the node where timeout occurred, (f) run record includes playbook source version hash (for version tracking), (g) querying runs by playbook_id returns all runs sorted by start time, (h) run record includes start_time, end_time, and per-node durations | 5.2.9 |

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
| 5.3.8 | Add tests: Event-to-playbook scope matching per [[playbooks#7. Event System]] Event-to-Scope Matching. Cases: (a) `task.completed` event with `project_id="myapp"` triggers project-scoped playbook for myapp AND system-scoped playbooks, (b) `task.completed` event with `project_id="myapp"` does NOT trigger project-scoped playbook for a different project, (c) event without `project_id` triggers only system-scoped playbooks, (d) agent-type-scoped playbook triggers only when event's agent matches that type, (e) multiple playbooks subscribed to same event type all trigger (not just first), (f) playbook with trigger event type that never fires does not interfere with other playbooks, (g) unrecognized event type does not cause errors in PlaybookManager | 5.3.3 |
| 5.3.9 | Add tests: Playbook cooldown per [[playbooks#6. Execution Model]] Concurrency. Cases: (a) playbook with 60s cooldown that just completed ignores trigger event within 60s, (b) same playbook triggers normally after cooldown expires, (c) cooldown is per-playbook — different playbooks with same trigger event are independent, (d) cooldown is tracked per scope (project-level cooldown does not block system-level for same playbook template), (e) playbook that fails still applies cooldown (prevents error loops), (f) cooldown of 0 means no cooldown (every event triggers), (g) concurrent events during cooldown are dropped (not queued) | 5.3.4 |
| 5.3.10 | Add tests: Timer service per [[playbooks#7. Event System]] Timer Service. Cases: (a) playbook with `trigger: timer:30m` receives synthetic timer event every 30 minutes, (b) timer interval is respected within reasonable tolerance (+/- 5 seconds), (c) minimum 1-minute interval is enforced — `timer:30s` is rejected or upgraded to 1m, (d) multiple timer-triggered playbooks with different intervals each fire at their own cadence, (e) timer continues firing after playbook run completes (recurring), (f) timer stops firing when playbook is removed/disabled, (g) system restart resumes timers from configuration (not from last fire time — fires immediately if overdue) | 5.3.7 |
| 5.3.11 | Add tests: Playbook composition via event chaining per [[playbooks#10. Composability]] Event Payload Filtering. Cases: (a) playbook A completes and emits `playbook.run.completed` with `playbook_id="code-review"` — playbook B subscribed with filter `{"playbook_id": "code-review"}` triggers, (b) playbook B does NOT trigger for `playbook.run.completed` from a different playbook_id, (c) 3-playbook chain: A → B → C each triggered by predecessor's completion event, (d) composition with payload data: playbook A's output is available in playbook B's trigger event payload, (e) circular composition (A triggers B triggers A) is prevented by cooldown or detected and blocked, (f) failed playbook emits `playbook.run.failed` — downstream playbooks subscribed to failure events trigger correctly, (g) composition across scopes: system playbook triggers project playbook via filtered event | 5.3.6, 0.1.2 |

### 5.4 Human-in-the-Loop

**Spec:** [[playbooks#9. Human-in-the-Loop]]

| # | Task | Depends On |
|---|---|---|
| 5.4.1 | Implement `wait_for_human` node: persist run state to DB, pause execution per [[playbooks#9. Human-in-the-Loop]] Pause and Resume | 5.2.9 |
| 5.4.2 | Implement notification for human review via [[messaging/discord|Discord]] / [[messaging/telegram|Telegram]] (with accumulated context summary) | 5.4.1 |
| 5.4.3 | Implement `human.review.completed` event handling: resume run from saved conversation state per [[playbooks#9. Human-in-the-Loop]] | 5.4.1 |
| 5.4.4 | Implement timeout for paused runs (configurable, default 24h, transition to timeout node or fail) per [[playbooks#9. Human-in-the-Loop]] Timeout | 5.4.1 |
| 5.4.5 | Implement `resume_playbook` command per [[playbooks#15. Playbook Commands]] | 5.4.3 |
| 5.4.6 | Add tests: Human-in-the-loop pause and resume per [[playbooks#9. Human-in-the-Loop]]. Cases: (a) playbook reaching `wait_for_human` node persists run state to DB and pauses with status "paused", (b) notification is sent via Discord/Telegram with context summary of what the playbook has done so far, (c) `human.review.completed` event resumes the run from the exact saved conversation state, (d) resumed run continues to the next node with human's input appended to conversation history, (e) human can provide structured input (approve/reject/feedback) that influences the transition, (f) `resume_playbook` command with run_id resumes the correct paused run, (g) multiple paused runs can coexist — resuming one does not affect others, (h) run state survives system restart (persisted to DB, not just in-memory) | 5.4.3 |
| 5.4.7 | Add tests: Paused playbook timeout per [[playbooks#9. Human-in-the-Loop]] Timeout. Cases: (a) paused run exceeding default 24h timeout transitions to "timed_out" status, (b) custom timeout (e.g., 1h) is respected — run times out after 1h, not 24h, (c) timed-out run transitions to timeout node if one is defined in the playbook graph, (d) timed-out run with no timeout node transitions to "failed" status, (e) resuming a timed-out run is rejected with clear error message, (f) timeout notification is sent to the same channel as the original pause notification, (g) timeout countdown resets if human provides partial input and re-pauses | 5.4.4 |

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
| 5.5.8 | Add tests: Playbook commands per [[playbooks#15. Playbook Commands]]. Cases: (a) `compile_playbook` with valid markdown returns success and compiled JSON path, (b) `compile_playbook` with invalid markdown returns error with details, (c) `dry_run_playbook` simulates execution with mock event and returns node trace without side effects (no DB changes, no events emitted), (d) `show_playbook_graph` returns ASCII or mermaid representation with correct nodes and transitions, (e) `list_playbooks` returns all playbooks across scopes with status (active/error) and last run time, (f) `list_playbook_runs` returns recent runs with status and node path taken, (g) `inspect_playbook_run` returns full node trace, conversation history, and token usage for a specific run, (h) all commands return `{"success": bool, ...}` dict format per command handler convention, (i) commands with invalid arguments return helpful error messages | 5.5.7 |

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

### 5.7 Observability

**Spec:** [[playbooks#14. Dashboard Visualization]], [[playbooks#19. Open Questions]] #4

| # | Task | Depends On |
|---|---|---|
| 5.7.1 | Implement playbook health metrics: tokens per node, run duration, transition paths, failure rates per [[playbooks#19. Open Questions]] #4 | 5.2.9 |
| 5.7.2 | Design dashboard playbook graph view (nodes as boxes, transitions as arrows, live state highlighting) per [[playbooks#14. Dashboard Visualization]] | 5.7.1 |

> **Test checkpoint:** Full system test with playbooks running alongside hooks.
> Trigger `task.completed` — verify both the old hook AND the new playbook fire.
> Compare outputs. Verify token budgets tracked correctly. Test a human-in-the-loop
> playbook end-to-end via Discord/Telegram. Verify timer-based playbooks fire on
> schedule. Test composition: playbook A completes → playbook B triggers via filtered
> event. This is the biggest validation point in the entire roadmap.
> Specific validations:
> - Coexistence: `task.completed` fires both hook-engine rule and playbook — compare outputs for equivalence
> - Compilation: all 4 default playbooks (task-outcome, system-health-check, codebase-inspector, dependency-audit) compile without errors
> - Execution: task-outcome playbook runs 3-node graph to completion on `task.completed`
> - Branching: task-outcome playbook takes different branch on success vs. failure tasks
> - Token budget: set a 1000-token budget, run playbook — verify it stops gracefully on exceed
> - Daily cap: set low daily cap, run multiple playbooks — verify cap enforced and resets
> - Human-in-the-loop: run a playbook that pauses for review, send approval via Discord, verify resume
> - Timeout: run same playbook with 5-second timeout, do NOT approve — verify timeout transition
> - Timer: install system-health-check with 30m timer, verify it fires (use mock clock in test)
> - Composition: task-outcome emits `playbook.run.completed` → downstream playbook triggers
> - Cooldown: trigger same playbook twice in 10 seconds — second trigger is ignored
> - Scope isolation: project playbook does not fire for events from a different project
> - Version pinning: recompile playbook while a run is in-flight — in-flight uses old version
> - Persistence: kill and restart system mid-run — paused runs resume, completed runs are in DB
> - Commands: `list_playbooks`, `list_playbook_runs`, `inspect_playbook_run` all return correct data
> - Migration validation: default playbooks produce equivalent results to the rules they replace

---

## Phase 6: Self-Improvement Loop

Close the loop: agents learn from experience.

**Source:** [[self-improvement]]

### 6.1 Reflection Playbooks

**Spec:** [[self-improvement#2. The Loop]], [[memory-scoping#10. Reflection Playbook (Periodic Consolidation)]]

| #     | Task                                                                                                                                                                             | Depends On   |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| 6.1.1 | Write coding agent reflection playbook (`vault/agent-types/coding/playbooks/reflection.md`) per [[self-improvement#2. The Loop]]                                                 | 5.1.2        |
| 6.1.2 | Write generic agent reflection playbook template for other agent types                                                                                                           | 6.1.1        |
| 6.1.3 | Implement reflection playbook trigger on `task.completed` for matching agent type per [[playbooks#8. Scoping]] agent-type scope                                                  | 5.3.3, 6.1.1 |
| 6.1.4 | Implement memory consolidation within reflection: merge duplicates, update outdated, promote cross-scope per [[memory-scoping#10. Reflection Playbook (Periodic Consolidation)]] | 6.1.3, 3.4.1 |
| 6.1.5 | Verify reflection playbook reads task records, extracts patterns, writes insights to agent-type memory                                                                           | 6.1.3        |

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
| 6.3.5 | Add tests: Reference stub regeneration per [[vault#4. Reference Stubs for External Docs]]. Cases: (a) changing a spec file in workspace triggers stub regeneration in `vault/projects/{id}/references/`, (b) regenerated stub summary reflects the new content (not stale), (c) stub retains Obsidian-compatible frontmatter and wikilink format, (d) stub file name matches source file name (e.g., `api-spec.md` → `api-spec.md` stub), (e) multiple spec files changed simultaneously each get their own stub regenerated, (f) stub generation handles large spec files (>5000 tokens) by summarizing effectively, (g) git-based detection: only files changed since last indexed commit trigger regeneration | 6.3.2 |
| 6.3.6 | Add tests: Reference stub source_hash caching per [[vault#7. Open Questions]] #2. Cases: (a) unchanged spec file does NOT trigger stub regeneration (source_hash matches), (b) touching file without content change does NOT trigger regeneration, (c) source_hash is persisted across system restarts, (d) stale stub detection: manually editing source file without triggering watcher flags stub as potentially stale, (e) force-regenerate command bypasses hash check, (f) deleting source file flags corresponding stub as orphaned | 6.3.3 |

### 6.4 Orchestrator Memory

**Spec:** [[self-improvement#5. Orchestrator Memory]]

| # | Task | Depends On |
|---|---|---|
| 6.4.1 | Implement startup scan of `vault/projects/*/README.md` → generate orchestrator summaries in `vault/orchestrator/memory/project-{id}.md` per [[self-improvement#5. Orchestrator Memory]] | 1.1.1, 2.2.5 |
| 6.4.2 | Wire README file watcher from Phase 1.3 to orchestrator re-summary per [[self-improvement#5. Orchestrator Memory]] On README change | 1.3.5, 6.4.1 |
| 6.4.3 | Add tests: Orchestrator memory from project READMEs per [[self-improvement#5. Orchestrator Memory]]. Cases: (a) creating a new `vault/projects/myapp/README.md` triggers generation of `vault/orchestrator/memory/project-myapp.md` summary, (b) summary captures key project details (tech stack, purpose, status) from the README, (c) editing README triggers summary update — new content reflected in updated summary, (d) startup scan processes all existing READMEs and creates/updates summaries for each, (e) project with no README does not cause errors (skipped gracefully), (f) deleting README flags orchestrator summary as potentially stale (or removes it), (g) summary is concise enough to fit in orchestrator's context alongside other project summaries | 6.4.2 |

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
> Specific validations:
> - Reflection playbook triggers on `task.completed` for each agent type that has one configured
> - After 5 completed tasks: at least 3 insights extracted and saved to agent-type memory collection
> - Duplicate insights (>0.95 similarity) are merged — collection size does not grow unboundedly
> - Retrieval counts increment each time an insight is returned by `memory_search`
> - `memory_health` command shows accurate collection sizes, growth rates, and retrieval hit rates
> - Stale memories (not retrieved in configurable N days) are flagged in health report
> - Contradictions between two memories on same topic are detected and tagged `#contested`
> - Reflection playbook surfaces stale/contradicted memories for review in its output
> - Orchestrator summaries for all active projects are current (updated within last README change)
> - Reference stubs are regenerated for changed workspace specs and stale stubs are flagged
> - Log analysis playbook writes operational insights to orchestrator memory collection
> - Memory consolidation in reflection merges cross-scope duplicates and promotes reusable patterns

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
| 7.1.5 | Add tests: Workflow CRUD and lifecycle per [[agent-coordination#6. Workflow Runtime]] Workflow State. Cases: (a) create workflow returns valid workflow_id and initial status "pending", (b) associate tasks with workflow via `workflow_id` FK — tasks queryable by workflow, (c) workflow status transitions: pending → running → completed, pending → running → failed, (d) invalid status transitions (e.g., completed → running) are rejected, (e) `workflow.stage.completed` event emitted when a stage's tasks all complete, (f) workflow with no associated tasks can still be created and tracked, (g) deleting a workflow does not delete its associated tasks (tasks survive independently), (h) concurrent workflow creation does not produce ID collisions | 7.1.3 |

### 7.2 Coordination Commands

**Spec:** [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] — The Interface Between Them

| # | Task | Depends On |
|---|---|---|
| 7.2.1 | Extend `create_task` command with `agent_type`, `affinity_agent_id`, `workspace_mode` parameters per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] | 7.1.1 |
| 7.2.2 | Implement `set_project_constraint` command (exclusive access, max agents by type, pause scheduling) per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] | — |
| 7.2.3 | Implement `release_project_constraint` command | 7.2.2 |
| 7.2.4 | Implement constraint enforcement in scheduler (check before assignment) per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]] What the Scheduler Owns | 7.2.2 |
| 7.2.5 | Add tests: Project constraint enforcement per [[agent-coordination#5. How Coordination Playbooks Change the Scheduler]]. Cases: (a) `set_project_constraint` with `exclusive=true` blocks scheduler from assigning any tasks to other agents on that project, (b) `release_project_constraint` lifts the block — scheduler resumes normal assignment, (c) constraint with `max_agents={"coding": 2}` allows up to 2 coding agents but blocks a third, (d) `pause_scheduling=true` constraint stops all task assignment for that project, (e) constraint on project A does not affect scheduling for project B, (f) attempting to set constraint on non-existent project returns clear error, (g) multiple constraints on same project stack correctly (e.g., exclusive + max_agents), (h) constraint persists across scheduler tick cycles until explicitly released | 7.2.4 |

### 7.3 Agent Affinity

**Spec:** [[agent-coordination#3. Core Concepts]] Agent Affinity, [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation

| # | Task | Depends On |
|---|---|---|
| 7.3.1 | Add `affinity_agent_id` and `affinity_reason` fields to tasks table per [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation | 7.2.1 |
| 7.3.2 | Implement scheduler affinity logic: prefer idle affinity agent, bounded wait up to N seconds, fallback per [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation | 7.3.1 |
| 7.3.3 | Implement agent type matching: task's `agent_type` field matched against agent's type during assignment per [[agent-coordination#3. Core Concepts]] Agent Affinity | 7.3.1 |
| 7.3.4 | Add tests: Agent affinity scheduling per [[agent-coordination#6. Workflow Runtime]] Agent Affinity Implementation. Cases: (a) task with `affinity_agent_id="agent-1"` is assigned to agent-1 when agent-1 is idle, (b) task with affinity to busy agent waits up to N seconds before falling back to another available agent, (c) fallback agent matches the task's `agent_type` requirement, (d) affinity with no wait time (N=0) immediately falls back if affinity agent is busy, (e) affinity agent that becomes idle within the wait window gets the task (not the fallback), (f) `affinity_reason` is logged for debugging (e.g., "original author of feature branch"), (g) task without affinity is assigned normally by the scheduler (no affinity preference) | 7.3.2 |
| 7.3.5 | Add tests: Agent type matching per [[agent-coordination#3. Core Concepts]] Agent Affinity. Cases: (a) task with `agent_type="code-review"` is NOT assigned to an agent with type "coding", (b) task with `agent_type="coding"` IS assigned to an available coding agent, (c) task with no `agent_type` is assigned to any available agent regardless of type, (d) task with `agent_type` that no agent matches stays queued (not assigned to wrong type), (e) agent with multiple type capabilities can match tasks of any of its types, (f) type mismatch rejection is logged with task_id, required_type, and agent_type for debugging | 7.3.3 |

### 7.4 Workspace Modes

**Spec:** [[agent-coordination#7. Workspace Strategy]]

| # | Task | Depends On |
|---|---|---|
| 7.4.1 | Add `lock_mode` field to workspace acquisition (default: `exclusive`) per [[agent-coordination#7. Workspace Strategy]] Lock Modes | — |
| 7.4.2 | Implement `branch-isolated` lock mode (multiple agents, same repo, different branches) per [[agent-coordination#7. Workspace Strategy]] | 7.4.1 |
| 7.4.3 | Implement git mutex for shared operations (fetch, gc) in branch-isolated mode per [[agent-coordination#7. Workspace Strategy]] | 7.4.2 |
| 7.4.4 | Add tests: Branch-isolated workspace mode per [[agent-coordination#7. Workspace Strategy]]. Cases: (a) two agents acquire workspace with `lock_mode="branch-isolated"` on same repo — both succeed, (b) each agent operates on a separate branch (no cross-branch interference), (c) shared git operations (fetch, gc) are serialized via mutex — concurrent fetches do not corrupt the repo, (d) agent A's commits on branch-A are not visible on agent B's branch-B, (e) branch-isolated lock is released when agent completes task, (f) three or more agents can work concurrently in branch-isolated mode, (g) branch-isolated mode with conflicting branches (same branch name) is rejected | 7.4.2 |
| 7.4.5 | Add tests: Exclusive workspace mode backward compatibility per [[agent-coordination#7. Workspace Strategy]] Lock Modes. Cases: (a) workspace acquired with `lock_mode="exclusive"` (default) blocks second agent from acquiring same workspace, (b) second agent's acquisition attempt waits or fails with clear error, (c) exclusive lock release allows next agent to acquire, (d) exclusive mode behavior is identical to pre-lock-mode behavior (backward compat), (e) workspace without explicit `lock_mode` defaults to exclusive, (f) mixing exclusive and branch-isolated on same repo is rejected (cannot downgrade from exclusive) | 7.4.1 |
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
| 7.5.7 | Add tests: Feature pipeline coordination playbook per [[agent-coordination#4. Coordination Playbook Examples]] Example 1. Cases: (a) feature-pipeline playbook creates coding task first, then review + QA tasks after coding completes, (b) review and QA tasks have dependency on coding task (not scheduled until coding is done), (c) review + QA tasks can run concurrently (no dependency between them), (d) merge task depends on both review AND QA completing, (e) task chain has correct `workflow_id` linking all tasks, (f) coding task has `agent_type="coding"`, review has `agent_type="code-review"`, QA has `agent_type="qa"`, (g) failure in coding task stops the pipeline (review + QA not created), (h) feature-pipeline fires on appropriate trigger event (e.g., `task.created` with type="feature") | 7.5.1 |
| 7.5.8 | Add tests: Review feedback cycle with agent affinity per [[agent-coordination#4. Coordination Playbook Examples]] Example 1. Cases: (a) reviewer marks code review as "changes_requested" — playbook creates a fix task, (b) fix task has `affinity_agent_id` set to the original coding agent (who wrote the code), (c) `affinity_reason` is "original author" or similar descriptive string, (d) if original agent is idle, fix task is assigned to them immediately, (e) if original agent is busy, fix task waits up to configured timeout then falls back, (f) fix task completion re-triggers review (loop back in playbook graph), (g) maximum review cycles are bounded (configurable, e.g., 3 rounds) to prevent infinite loops | 7.5.1, 7.3.2 |
| 7.5.9 | Add tests: Exploration coordination playbook per [[agent-coordination#4. Coordination Playbook Examples]] Example 2. Cases: (a) exploration playbook creates N parallel research tasks with no dependencies between them, (b) all parallel tasks are assigned to available agents concurrently (scheduler respects independence), (c) reviewer task is created only after ALL parallel tasks complete (depends on all), (d) reviewer task receives summaries/outputs from all parallel tasks as context, (e) partial failure (2 of 3 parallel tasks complete, 1 fails) — reviewer still triggers with available results plus failure note, (f) exploration with single parallel task degrades gracefully to sequential, (g) workflow status reflects "running" until reviewer completes, then "completed" | 7.5.4 |
| 7.5.10 | Add tests: Orphan workflow recovery per [[agent-coordination#11. Open Questions]] #2. Cases: (a) kill coordination playbook mid-workflow — in-flight tasks continue executing to completion, (b) tasks created before crash have correct dependencies and are scheduled normally, (c) re-triggering coordination playbook discovers existing workflow and resumes from current state, (d) resumed playbook does not re-create tasks that already exist, (e) workflow status shows "running" during orphan period (not "failed"), (f) orphan detection: system identifies workflows with no active playbook run and alerts operator, (g) manual `resume_playbook` can restart coordination from the last completed stage | 7.5.6 |

### 7.6 Coordination Observability

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
> Specific validations:
> - Create FEATURE task: feature-pipeline playbook triggers and creates coding task with `agent_type="coding"`
> - Coding agent completes and pushes PR: `git.pr.created` event fires, playbook creates review + QA tasks
> - Review task assigned to `code-review` agent, QA task assigned to `qa` agent — type matching enforced
> - Review + QA run concurrently (scheduler assigns both when agents available, respects DAG independence)
> - Reviewer requests changes: fix task created with `affinity_agent_id` = original coding agent
> - Original coding agent receives fix task (was idle) — verify affinity scheduling
> - Fix task completes, re-review passes, merge task created — full pipeline completes
> - Workflow status transitions: pending → running → completed, all tasks have correct `workflow_id`
> - Project constraint: set `exclusive=true` mid-workflow — no new tasks scheduled for project until released
> - Branch-isolated mode: coding and QA agents work on same repo simultaneously on different branches
> - Exclusive mode: attempt concurrent access to exclusive workspace — second agent blocked
> - Exploration playbook: create 3 parallel research tasks, verify all complete before reviewer starts
> - Orphan recovery: kill playbook mid-pipeline, verify tasks continue, re-trigger resumes from last stage
> - Scheduler type mismatch: QA task is NOT assigned to coding agent even if coding agent is idle
> - Affinity fallback: make affinity agent busy, verify fallback to another agent of same type after timeout
> - End-to-end timing: full feature pipeline completes within reasonable time (measure bottlenecks)

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
| 8.7 | ~~Update all specs to remove "future evolution" callouts (they're now current)~~ ✅ | 8.4 |
| 8.8 | Remove deprecated spec files (`proactive-inspector.md` already removed, verify no others) | 8.7 |

> **Final test checkpoint:** Full regression test. Every feature that worked with
> hooks still works with playbooks. Memory operations all route through v2 plugin.
> No references to hook engine in active code paths. Run entire test suite.
> Specific validations:
> - All existing hook-driven behaviors (post-action-reflection, spec-drift-detector, error-recovery-monitor) work via playbooks
> - Side-by-side comparison: run same task with hooks and with playbooks — outputs are functionally equivalent
> - Passive rules migrated to vault memory files are searchable via `memory_search`
> - Hook commands (`list_hooks`, `create_hook`, etc.) redirect to playbook equivalents with deprecation notice
> - No imports of `HookEngine`, `RuleManager`, or `src/memory.py` MemoryManager in any active code path
> - Hook/rule DB tables are dropped in Alembic migration without data loss (migration verified both up and down)
> - All memory operations route through v2 plugin — v1 plugin is unregistered and removed
> - Full test suite passes: `pytest tests/` with zero failures and no deprecation warnings from removed code
> - Spec files updated: no "future evolution" callouts remain that reference now-implemented features
> - No orphaned spec files (e.g., `proactive-inspector.md`) exist in the specs directory
> - System boots cleanly from fresh install (no old paths, no migration needed, all defaults are playbook-based)

---

## Summary

| Phase | Tasks | Status | Key Deliverable | Source Specs | Depends On |
|---|---|---|---|---|---|
| **0** | 23 | ✅ Complete | Prerequisite refactors | [[playbooks]] §17 | — |
| **1** | 19 | ✅ Complete | Vault structure + file watcher | [[vault]] | — |
| **2** | 38 | 🔵 Ready | memsearch fork + memory plugin v2 | [[memory-plugin]], [[memory-scoping]] §7 | ✅ Phase 1 |
| **3** | 27 | ⚪ Blocked | Memory scoping, tiers, overrides, dedup | [[memory-scoping]] | Phase 2 |
| **4** | 18 | ⚪ Blocked | Profiles as markdown | [[profiles]] | ✅ Phase 1, Phase 3 |
| **5** | 48 | 🔵 Ready | Playbook system | [[playbooks]] | ✅ Phase 0, ✅ Phase 1 |
| **6** | 18 | ⚪ Blocked | Self-improvement loop | [[self-improvement]], [[vault]] §4 | Phase 3, Phase 5 |
| **7** | 27 | ⚪ Blocked | Agent coordination | [[agent-coordination]] | Phase 5 |
| **8** | 8 | ⚪ Blocked | Hook engine deprecation | [[playbooks]] §13 | Phase 5, Phase 6 |
| **Total** | **226** | 42 done | | | |

### Parallelism Opportunities

Phases 0 and 1 are complete.
Phase 2 and Phase 5 can now run in parallel (both dependencies met).
Phase 4 can start as soon as Phase 3.1 lands.
Phase 6 requires both Phase 3 and Phase 5.
Phase 7 requires Phase 5.

```
Phase 0 ✅ ───────────────────────► Phase 5 🔵 ──► Phase 7
                                        │              │
Phase 1 ✅ ──► Phase 2 🔵 ──► Phase 3 ──► Phase 6    Phase 8
                                  │
                              Phase 4
```
