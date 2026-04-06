---
auto_tasks: true
---

# Proactive Codebase Inspector — Implementation Plan

## Background

The current suggestion system only activates reactively — after task completion
(post-action reflection, spec drift), after failures (error recovery), or in response
to Discord conversations (chat observer). The user wants a system that **periodically
and randomly inspects** sections of source code, docs, specs, and tests, analyzes them
for potential improvements, and decides whether to surface a suggestion.

### Design Reference

Full specification: `specs/proactive-inspector.md`

### Key Design Decisions

- **Implemented as a default global rule** using the existing rule → hook → supervisor
  pipeline. No new subsystem needed.
- **Random target selection** with weighted categories (source 40%, docs/specs 20%,
  tests 15%, config 10%, recently modified 15%).
- **Suggestions delivered through existing infrastructure** — `chat_analyzer_suggestions`
  table, `SuggestionView` Discord UI, hash-based deduplication.
- **Inspection history** tracked in project memory to ensure broad coverage over time.
- **High decision threshold** — the LLM is instructed to prefer silence over noise,
  only suggesting genuinely actionable findings.

---

## Phase 1: Add the default rule and inspector prompt template

Create the proactive codebase inspector as a new default global rule and its
supporting prompt template.

**Files to create/modify:**
- `src/prompts/default_rules/proactive-codebase-inspector.md` — New default rule
  with Intent, Trigger (every 4 hours), and Logic sections defining the inspection
  process (target selection, analysis dimensions, decision threshold, suggestion
  delivery)
- Verify the rule installs correctly via `install_defaults()` by checking that
  `RuleManager` picks it up and generates hooks

**Acceptance criteria:**
- Rule file exists in `src/prompts/default_rules/`
- Running `install_defaults()` copies it to global rules directory
- Reconciliation generates a periodic hook per active project
- The hook fires on the configured interval and the supervisor receives the
  inspector prompt with full tool access

## Phase 2: Implement inspection history tracking

Add lightweight inspection history tracking so the inspector achieves broad codebase
coverage rather than repeatedly inspecting popular files.

**Files to create/modify:**
- `src/memory.py` — Add methods to MemoryManager:
  - `record_inspection(project_id, file_path, finding_count)` — Append to
    inspection history JSON
  - `get_inspection_history(project_id, days=30)` — Read recent history
  - `get_uninspected_files(project_id, all_files, days=14)` — Return files not
    recently inspected (for weighted selection bias)
  - Prune entries older than `history_retention_days`
- Storage location: `~/.agent-queue/memory/{project_id}/inspector_history.json`

**Acceptance criteria:**
- History file is created and updated on each inspection
- Old entries are pruned automatically
- The inspector prompt can reference inspection history to bias toward uninspected
  files

## Phase 3: Add inspector-specific tools for target selection

Give the supervisor structured tools for the inspection workflow rather than relying
entirely on shell commands for file discovery and random selection.

**Files to create/modify:**
- `src/tool_registry.py` or a new `src/tools/inspector_tools.py` — Register tools:
  - `inspect_random_file` — Lists workspace files (respecting .gitignore and
    exclusion patterns), applies weighted category selection, checks inspection
    history, returns the selected file path and content (or section for large files)
  - `record_inspection_result` — Records what was inspected and whether findings
    were produced, updates inspection history
- These tools are available to the supervisor during hook execution and compose
  with existing `create_task` and suggestion tools

**Acceptance criteria:**
- `inspect_random_file` returns a random file with content, weighted by category
- Large files are automatically sectioned (~150 lines)
- Recently inspected files are deprioritized
- `record_inspection_result` persists to inspection history

## Phase 4: Test the full inspection cycle end-to-end

Write tests and verify the complete flow: rule → hook → target selection → analysis →
suggestion (or skip) → history recording.

**Files to create/modify:**
- `tests/test_proactive_inspector.py` — Tests covering:
  - Rule installation and hook generation
  - Target selection with category weighting
  - Inspection history recording and pruning
  - Large file sectioning
  - Deduplication of repeated findings
  - Integration test: mock LLM returns a finding → suggestion appears in DB
  - Integration test: mock LLM returns skip → no suggestion, history updated
- Manual end-to-end test: fire the hook manually via `fire_hook` and verify Discord
  output

**Acceptance criteria:**
- All unit tests pass
- Manual fire produces a Discord suggestion embed (or clean skip log)
- No token budget regressions (inspection stays within hook token limits)
