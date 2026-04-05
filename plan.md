---
auto_tasks: true
---

# Memory Consolidation System — Implementation Plan

> Design spec: `specs/memory-consolidation.md`
> Problem: Memory is bulk task storage; users can't get quick answers about project metadata
> Solution: Layered knowledge system with structured factsheets, topic-organized knowledge base, and periodic consolidation

## Phase 1: Project Factsheet — Structured Metadata Layer

**Goal:** Add a `factsheet.md` per project that surfaces key metadata (URLs, tech stack, contacts) as YAML frontmatter for instant lookup.

**Files to create/modify:**
- `src/memory.py` — Add `read_factsheet()`, `write_factsheet()`, `update_factsheet_field()` methods to `MemoryManager`
- `src/models.py` — Add `factsheet: str = ""` field to `MemoryContext` dataclass; add `ProjectFactsheet` dataclass for typed access to factsheet fields
- `src/prompts/memory_consolidation.py` — New file with `FACTSHEET_SEED_TEMPLATE` (initial YAML structure), `FACTSHEET_EXTRACTION_PROMPT`
- Extend `build_context()` in `memory.py` to load factsheet as Tier 0 (before profile)
- Extend `MemoryContext.to_context_block()` in `models.py` to render factsheet section
- Bootstrap: auto-populate `urls.github` from `projects.repo_url` database field when factsheet is first created

**Tests:**
- `tests/test_memory_consolidation.py` — factsheet read/write/update, YAML parsing, context injection

## Phase 2: Post-Task Fact Extraction

**Goal:** After each task completion, extract structured facts (URLs, tech stack, decisions) into staging files for later consolidation.

**Files to create/modify:**
- `src/memory.py` — Add `extract_task_facts()` method that runs alongside existing `revise_profile()` and `generate_task_notes()`
- `src/prompts/memory_consolidation.py` — Add `FACT_EXTRACTION_SYSTEM_PROMPT` and `FACT_EXTRACTION_USER_PROMPT`
- `src/orchestrator.py` — Call `extract_task_facts()` after task completion (near line 4447 where `generate_task_notes` is called)
- `src/config.py` — Add `fact_extraction_enabled: bool = True` to `MemoryConfig`
- Staging files written to `memory/{project_id}/staging/{task_id}.json`

**Tests:**
- Test fact extraction with mock LLM responses
- Test staging file format and storage

## Phase 3: Knowledge Base Topic Files

**Goal:** Create topic-organized knowledge files (`knowledge/architecture.md`, etc.) that contain sourced facts with deep-dive links.

**Files to create/modify:**
- `src/memory.py` — Add `read_knowledge_topic()`, `list_knowledge_topics()` methods
- `src/prompts/memory_consolidation.py` — Add `KNOWLEDGE_TOPIC_SEED_TEMPLATES` for each topic category
- `src/config.py` — Add `knowledge_topics` list and `index_knowledge: bool = True` to `MemoryConfig`
- Extend memory indexing to include `knowledge/` directory alongside `notes/`
- Knowledge files follow format: heading + bullet facts + source references

**Tests:**
- Test knowledge topic CRUD
- Test vector index includes knowledge files

## Phase 4: Daily Consolidation Process

**Goal:** Scheduled task that processes staging files into factsheet updates and knowledge base entries.

**Files to create/modify:**
- `src/memory.py` — Add `run_daily_consolidation()` method that reads staging, updates factsheet YAML and knowledge topics
- `src/prompts/memory_consolidation.py` — Add `DAILY_CONSOLIDATION_SYSTEM_PROMPT` and `DAILY_CONSOLIDATION_USER_PROMPT`
- `src/config.py` — Add `consolidation_enabled`, `consolidation_schedule`, `consolidation_provider`, `consolidation_model` to `MemoryConfig`
- Register consolidation as a periodic hook in `src/orchestrator.py` (or provide a manual `/consolidate` command)
- Staging files move to `staging/processed/` after consolidation

**Tests:**
- Test end-to-end: staging files → factsheet update → knowledge base update
- Test deduplication logic
- Test conflict resolution (newer facts win)

## Phase 5: Agent Tools and Query Patterns

**Goal:** Expose factsheet and knowledge base to agents via new tools, update supervisor to check factsheet first.

**Files to create/modify:**
- `src/plugins/internal/memory.py` — Add `project_factsheet` tool (view/update), `project_knowledge` tool (read topics), `search_all_projects` tool (cross-project metadata search)
- `src/supervisor.py` — Update supervisor prompts to check factsheet before creating tasks for metadata questions
- `src/prompts/` — Update relevant supervisor/agent prompts to reference factsheet and knowledge base

**Tests:**
- Test new tool invocations
- Test cross-project search
- Test supervisor prompt includes factsheet guidance

## Phase 6: Weekly Deep Consolidation and Bootstrap

**Goal:** Weekly process that reviews/prunes knowledge, plus one-time bootstrap for existing projects.

**Files to create/modify:**
- `src/memory.py` — Add `run_deep_consolidation()` (prune stale, resolve conflicts, regenerate factsheet summary) and `bootstrap_consolidation()` (generate initial factsheet + knowledge from existing memories)
- `src/prompts/memory_consolidation.py` — Add deep consolidation and bootstrap prompts
- `src/config.py` — Add `deep_consolidation_schedule` to `MemoryConfig`
- `src/plugins/internal/memory.py` — Add `consolidate` tool for manual trigger with `--bootstrap` flag

**Tests:**
- Test bootstrap from existing task memories
- Test stale fact pruning
- Test full weekly consolidation cycle
