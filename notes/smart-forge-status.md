# Smart Forge (5-Phase Memory Integration) — Implementation Status

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

**Investigation date:** 2026-03-27
**Task:** fresh-torrent/investigate-smart-forge-implementation-status

## TL;DR

All five phases are **implemented in code**. No new implementation plan is needed. The gap is **operational configuration** — profiles aren't being generated despite the code being wired up, compaction is disabled by default, and auto-note generation is off.

---

## Phase-by-Phase Status

### Phase 1: Project Profiles ✅ Implemented

- **Config**: `profile_enabled: True` (on by default)
- **Code**: `MemoryManager.get_profile()`, `update_profile()`, `_profile_path()` in `src/memory.py`
- **Storage**: `{data_dir}/memory/{project_id}/profile.md`
- **Gap**: **No `profile.md` files exist on disk yet** — searched both `/home/jkern/.agent-queue/memory/` and `/home/jkern/agent-queue-workspaces/memory/`

### Phase 2: Post-Task Profile Revision ✅ Implemented

- **Config**: `revision_enabled: True` (on by default)
- **Code**: `MemoryManager.revise_profile()` and `_build_revision_prompt()` in `src/memory.py`
- **Integration**: Called from `orchestrator.py:3571-3579` after every successful task completion
- **Prompts**: Full templates in `src/prompts/memory_revision.py`
- **Extra**: `regenerate_profile()` command for full profile regeneration from history; `promote_note_to_profile()` for note integration

### Phase 3: Notes Integration ✅ Implemented

- **Config**: `auto_generate_notes: False` (intentionally off by default), `notes_inform_profile: True`
- **Bidirectional flow**:
  - **Notes → Profile**: Recent notes included in revision context; `promote_note_to_profile()`; note writes trigger profile revision via `_trigger_note_profile_revision()`
  - **Tasks → Notes**: `generate_task_notes()` auto-generates categorized notes from completed tasks (when enabled)
- **Integration**: Wired in orchestrator post-task flow and command handler note operations

### Phase 4: Memory Compaction ✅ Implemented

- **Config**: `compact_enabled: False` (disabled by default)
- **Code**: `MemoryManager.compact()` — three-tier strategy:
  - Recent (< `compact_recent_days`/7d): kept as-is
  - Medium (7d–30d): LLM-summarized into weekly digests
  - Old (> `compact_archive_days`/30d): deleted after digesting
- **Integration**: `orchestrator._periodic_memory_compact()` in main loop, rate-limited by `compact_interval_hours`
- **Manual trigger**: `compact_memory` command available

### Phase 5: Tiered Context Delivery ✅ Implemented

- **Code**: `MemoryManager.build_context()` in `src/memory.py:901-1030`
- **Tiers** (priority order):
  1. Project profile
  1.5. Project documentation (CLAUDE.md, README.md)
  2. Relevant notes (semantic search)
  3. Recent task memories (last N by mtime)
  4. Semantic search results (de-duplicated)
- **Config**: `context_max_tokens: 4000`, `context_include_recent: 3`

---

## Summary Table

| Phase | Status | Default Config | Gap |
|-------|--------|---------------|-----|
| 1. Project Profiles | ✅ Complete | Enabled | No profiles generated yet |
| 2. Post-Task Revision | ✅ Complete | Enabled | Depends on LLM provider config |
| 3. Notes Integration | ✅ Complete | Partial (auto-gen off) | Intentional — reduces noise |
| 4. Memory Compaction | ✅ Complete | **Disabled** | Needs explicit opt-in |
| 5. Tiered Context | ✅ Complete | Enabled | Working |

---

## Why No Profiles Exist Yet

Despite Phases 1-2 being enabled by default, no `profile.md` files have been generated. Likely causes:

1. **LLM provider not configured for revision**: `revision_provider` and `revision_model` default to empty strings. The fallback chain goes to the chat provider, but if that's not available during the revision call, it silently fails with "No LLM provider available for profile revision".
2. **Silent failures**: The revision code catches all exceptions and logs warnings but doesn't surface errors to users.

## Recommended Actions (Operational, Not Code)

1. **Check logs** for "No LLM provider available for profile revision" or "Profile revision failed" warnings
2. **Bootstrap profiles**: Run `regenerate_profile` for each active project to create initial profiles from task history
3. **Enable compaction**: Set `compact_enabled: True` in config for projects with growing task history
4. **Consider auto-notes**: Enable `auto_generate_notes` for high-value projects where institutional knowledge capture matters
