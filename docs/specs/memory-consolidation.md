---
tags: [spec, memory, consolidation]
---

# Memory Consolidation System

> **Status:** Implemented
> **Author:** Agent (fleet-beacon)
> **Date:** 2026-04-05

See [[design/memory-scoping]] for scoped collections and [[design/memory-plugin]] for the plugin v2 architecture.

## Problem Statement

The memory system currently functions as bulk storage of completed task information. When a user asks "what's the GitHub URL for project X?" or "what stack does project Y use?", the answer may be buried across dozens of task memory files, weekly digests, or only discoverable through semantic search that may not surface it reliably.

### Current Pain Points

1. **Project metadata is scattered** — GitHub URLs, tech stacks, deployment info, and other key facts exist only in the `projects` DB table (limited fields) or buried in task memories
2. **No structured knowledge extraction** — task memories capture *what happened* but don't extract *reusable facts* into queryable structures
3. **Profile.md is too coarse** — the single profile document captures architecture/conventions but doesn't surface quick-lookup metadata (URLs, contacts, environments, dependencies)
4. **Compaction loses detail** — weekly digests summarize task history but don't extract structured facts before discarding individual memories
5. **No cross-project knowledge** — each project's memory is siloed; there's no way to query across projects ("which projects use PostgreSQL?")

## Design Goals

1. **Quick answers** — common questions (GitHub URL, tech stack, deploy URL, key contacts) should be answerable from structured metadata without semantic search
2. **Progressive distillation** — automatically extract and promote important facts from task memories into organized knowledge layers
3. **Deep-dive links** — condensed knowledge should link back to original sources (task IDs, file paths, log locations) for investigation
4. **Periodic consolidation** — scheduled tasks that review and organize accumulated knowledge
5. **Easy querying** — both agents and users should be able to find project knowledge through natural language and structured lookups

## Architecture

### Knowledge Layers

The consolidated memory system adds two new layers above the existing memory infrastructure:

```
┌─────────────────────────────────────────────────┐
│  Layer 4: Project Factsheet (NEW)               │  ← Quick-lookup structured metadata
│  Structured YAML/MD with key project facts      │
├─────────────────────────────────────────────────┤
│  Layer 3: Knowledge Base (NEW)                  │  ← Organized topic-based knowledge
│  Topic files with sourced facts + deep links    │
├─────────────────────────────────────────────────┤
│  Layer 2: Profile + Notes (existing)            │  ← Living project understanding
│  profile.md + notes/{category}/*.md             │
├─────────────────────────────────────────────────┤
│  Layer 1: Raw Memories (existing)               │  ← Task completions + digests
│  tasks/*.md + digests/*.md                      │
└─────────────────────────────────────────────────┘
```

### Layer 4: Project Factsheet

A structured YAML-frontmatter + markdown file at `memory/{project_id}/factsheet.md` that serves as the **quick-reference card** for a project. This is the first thing checked when answering metadata questions.

```yaml
---
# Auto-maintained by consolidation system
# Manual edits are preserved; consolidation merges, not overwrites
last_updated: "2026-04-05T14:30:00Z"
consolidation_version: 1

project:
  name: "Skinnable ImGui"
  id: "skinnable-imgui"
  description: "A skinning/theming system for Dear ImGui"

urls:
  github: "https://github.com/user/skinnable-imgui"
  docs: "https://skinnable-imgui.readthedocs.io"
  ci: "https://github.com/user/skinnable-imgui/actions"
  deploy: null

tech_stack:
  language: "C++"
  framework: "Dear ImGui"
  build_system: "CMake"
  test_framework: "Catch2"
  key_dependencies:
    - "Dear ImGui v1.90"
    - "nlohmann/json"
    - "stb_image"

environments:
  - name: "dev"
    url: null
    notes: "Local build only"

contacts:
  owner: "ElectricJack"

key_paths:
  source: "src/"
  tests: "tests/"
  config: "imgui.ini"
  entry_point: "src/main.cpp"
---

# Skinnable ImGui — Quick Reference

## What It Does
A theming/skinning system for Dear ImGui that allows runtime style switching
with JSON-based theme definitions.

## Current State
Active development. Core skinning engine works, theme editor UI in progress.

## Recent Focus Areas
- Theme file format finalization
- Editor panel for live preview
- Font management system
```

**Key design decisions:**
- YAML frontmatter for machine-readable structured data
- Markdown body for human-readable summary
- Fields are additive — consolidation merges new facts, doesn't remove manually-set ones
- The `urls.github` field directly answers the "what's the GitHub URL?" question
- `tech_stack` answers "what language/framework does X use?"
- `key_paths` helps agents navigate unfamiliar projects

### Layer 3: Knowledge Base

Topic-organized knowledge files at `memory/{project_id}/knowledge/{topic}.md` that contain **sourced, organized facts** extracted from task memories and notes. Each fact links back to its source.

**Directory structure:**
```
memory/{project_id}/knowledge/
├── architecture.md        # System architecture and design
├── api-and-endpoints.md   # API routes, protocols, integrations
├── deployment.md          # Deploy process, environments, CI/CD
├── dependencies.md        # External deps, version constraints
├── gotchas.md            # Known issues, workarounds, pitfalls
├── conventions.md         # Coding standards, naming, patterns
└── decisions.md          # Key technical decisions with rationale
```

**File format:**
```markdown
# Architecture Knowledge

> Last consolidated: 2026-04-05 | Sources: 14 tasks, 3 notes

## Core Architecture
- Event-driven async system with SQLAlchemy backend
  - *Source: task vivid-flare (2026-03-20)*
- Discord bot serves as primary UI; REST API for programmatic access
  - *Source: task emerald-peak (2026-03-15)*

## Data Flow
- Tasks flow through state machine: DEFINED → QUEUED → RUNNING → COMPLETED
  - *Source: task nimble-brook (2026-02-28), file: specs/models-and-state-machine.md*
- Memory context is assembled at task start via tiered priority system
  - *Source: task coral-dawn (2026-03-10), file: src/memory.py:912*

## Key Components
- **Orchestrator** — central coordinator, manages task lifecycle
  - *Source: profile.md, confirmed by 8 tasks*
- **Supervisor** — monitors chat, delegates to agents
  - *Source: task swift-river (2026-03-22)*
```

**Key design decisions:**
- Topics are predefined categories (not free-form) to ensure consistency
- Each fact has a source reference (task ID, file path, or note path)
- Source references enable deep-dive investigation
- Files are periodically regenerated/updated by the consolidation process
- Semantic search indexes these files alongside existing memory content

### Consolidation Process

#### When It Runs

Consolidation is a **periodic scheduled task** that runs via the existing hook/schedule system:

| Trigger | Action | Frequency |
|---------|--------|-----------|
| **Post-task** | Extract facts from completed task into staging | Every task completion |
| **Daily consolidation** | Process staged facts into knowledge base + update factsheet | Once daily (configurable) |
| **Weekly deep consolidation** | Full review — merge, deduplicate, prune stale facts | Weekly |

#### Post-Task Fact Extraction (Lightweight)

After each task completion (alongside existing `revise_profile` and `generate_task_notes`), a new **fact extraction** step runs:

```python
async def extract_task_facts(self, task, output, workspace_path):
    """Extract structured facts from a completed task.
    
    Writes extracted facts to memory/{project_id}/staging/{task_id}.json
    for later consolidation.
    """
```

**LLM prompt extracts:**
- URLs discovered or mentioned (GitHub, docs, CI, deploy)
- Technology/dependency information
- Architecture insights with file path references
- Key decisions made and their rationale
- New conventions established
- Known issues or workarounds discovered

**Output format (staging file):**
```json
{
  "task_id": "vivid-flare",
  "task_title": "Fix agent delegation for URL queries",
  "extracted_at": "2026-04-05T14:30:00Z",
  "facts": [
    {
      "category": "url",
      "key": "github",
      "value": "https://github.com/user/skinnable-imgui",
      "confidence": "high",
      "source_context": "Found in repo config and task description"
    },
    {
      "category": "architecture", 
      "key": "delegation-pattern",
      "value": "Supervisor delegates URL lookups to project metadata before creating tasks",
      "confidence": "medium",
      "source_context": "New pattern established by this task"
    }
  ]
}
```

#### Daily Consolidation Task

A scheduled task (hook with cron schedule) that:

1. **Reads all staging files** from `memory/{project_id}/staging/`
2. **Updates factsheet** — merges extracted URLs, tech stack info, contacts into `factsheet.md` YAML frontmatter
3. **Updates knowledge base** — routes facts to appropriate topic files in `knowledge/`
4. **Deduplicates** — merges facts that say the same thing from different sources (keeps all source references)
5. **Cleans staging** — moves processed staging files to `staging/processed/`

```python
async def run_daily_consolidation(self, project_id: str, workspace_path: str) -> dict:
    """Run daily knowledge consolidation for a project.
    
    Processes staging files, updates factsheet and knowledge base.
    Returns stats about facts processed and files updated.
    """
```

#### Weekly Deep Consolidation

A more thorough weekly process that:

1. **Reviews entire knowledge base** for each topic file
2. **Prunes stale facts** — facts whose sources are very old and have been superseded
3. **Resolves conflicts** — when facts contradict each other, keeps the most recent
4. **Regenerates summaries** — updates the markdown body of `factsheet.md` with current state
5. **Cross-references with database** — ensures factsheet URLs match `projects.repo_url`, etc.
6. **Computes coverage metrics** — identifies knowledge gaps (e.g., "no deployment info found")

### Integration with Existing Memory System

#### Context Delivery Enhancement

The existing `MemoryContext` dataclass gets a new tier:

```python
@dataclass
class MemoryContext:
    factsheet: str = ""        # NEW: Project factsheet (highest priority, Tier 0)
    profile: str = ""          # Project profile (Tier 1)
    project_docs: str = ""     # CLAUDE.md etc. (Tier 1.5)
    notes: str = ""            # Relevant notes (Tier 2)
    recent_tasks: str = ""     # Recent task summaries (Tier 3)
    search_results: str = ""   # Semantic search results (Tier 4)
    memory_folder: str = ""    # Path reference
```

The factsheet is injected as **Tier 0** — always included, never trimmed. It's small (typically <1KB of YAML + short markdown) and answers the most common questions.

#### build_context Enhancement

```python
async def build_context(self, project_id, task, workspace_path):
    ctx = MemoryContext()
    
    # NEW: Tier 0 — Factsheet (always included)
    factsheet_path = os.path.join(
        self._project_memory_dir(project_id), "factsheet.md"
    )
    if os.path.isfile(factsheet_path):
        with open(factsheet_path) as f:
            ctx.factsheet = f.read()
    
    # Existing tiers...
    # Tier 1: Profile
    # Tier 1.5: Project docs
    # Tier 2: Notes (now also searches knowledge/ directory)
    # Tier 3: Recent tasks
    # Tier 4: Semantic search (now also indexes knowledge/ files)
```

#### Knowledge Base Indexing

The `knowledge/` directory is indexed alongside `notes/` for semantic search. This means agents doing `memory_search` will find organized knowledge-base facts in addition to raw notes and task memories.

Add to `MemoryConfig`:
```python
index_knowledge: bool = True  # index knowledge base files
consolidation_enabled: bool = False  # enable periodic consolidation
consolidation_schedule: str = "0 3 * * *"  # daily at 3 AM
deep_consolidation_schedule: str = "0 4 * * 0"  # weekly Sunday 4 AM
fact_extraction_enabled: bool = True  # extract facts post-task
```

#### Querying Patterns

**Direct metadata lookup (via factsheet YAML parsing):**
```
User: "What's the GitHub URL for skinnable-imgui?"
→ Agent reads factsheet.md → parses YAML frontmatter → returns urls.github
```

**Topic-based knowledge lookup:**
```
User: "How does deployment work for agent-queue?"
→ Agent reads knowledge/deployment.md → returns organized deployment knowledge
```

**Semantic search (existing, now enhanced):**
```
User: "What did we decide about the database schema?"
→ memory_search → finds relevant facts in knowledge/decisions.md + task memories
```

**Cross-project queries (new tool):**
```
User: "Which projects use PostgreSQL?"
→ New tool reads all project factsheets → filters by tech_stack contents
```

### New Agent Tools

Add to the memory plugin (`src/plugins/internal/memory.py`):

#### `project_factsheet`
```python
{
    "name": "project_factsheet",
    "description": "View or update the project's quick-reference factsheet with key metadata (URLs, tech stack, contacts, etc.)",
    "parameters": {
        "action": "view | update",
        "updates": {
            "urls.github": "https://...",
            "tech_stack.language": "Python"
        }
    }
}
```

#### `project_knowledge`
```python
{
    "name": "project_knowledge",
    "description": "Read organized knowledge about a specific topic for this project",
    "parameters": {
        "topic": "architecture | api-and-endpoints | deployment | dependencies | gotchas | conventions | decisions",
        "query": "optional search within the topic"
    }
}
```

#### `search_all_projects`
```python
{
    "name": "search_all_projects",
    "description": "Search across all project factsheets for specific metadata",
    "parameters": {
        "query": "natural language query",
        "field": "optional specific field to search (e.g., 'urls.github', 'tech_stack.language')"
    }
}
```

### Prompts for Consolidation

#### Fact Extraction Prompt

```
You are a knowledge extraction system. Analyze the completed task and extract
structured facts that would be useful for quick project reference.

Extract:
1. **URLs** — any GitHub, documentation, CI/CD, deployment, or other project URLs
2. **Tech Stack** — languages, frameworks, build systems, key dependencies
3. **Architecture** — structural insights about the system design
4. **Decisions** — key technical decisions made, with rationale
5. **Conventions** — coding patterns, naming conventions, workflow standards
6. **Gotchas** — issues discovered, workarounds needed, things to avoid
7. **Paths** — important file paths, entry points, config locations

For each fact, assign a confidence level:
- high: explicitly stated or directly observable
- medium: strongly implied by context
- low: inferred, may need verification

Respond with a JSON object containing a "facts" array.
```

#### Daily Consolidation Prompt

```
You are a knowledge consolidation system. You receive newly extracted facts
and the current state of a project's knowledge base. Your job is to merge
the new facts into the existing knowledge, maintaining organization and
deduplicating.

Rules:
1. MERGE new facts into existing topic sections — don't just append
2. DEDUPLICATE — if a fact already exists, add the new source reference
3. PRESERVE source references — every fact must link to its origin
4. RESOLVE CONFLICTS — when facts disagree, prefer the most recent source
5. MAINTAIN STRUCTURE — keep the established heading hierarchy
6. BE CONCISE — prefer bullet points, link to sources for detail
```

### File Storage Layout (Updated)

```
~/.agent-queue/memory/{project_id}/
├── factsheet.md              # NEW: Structured project metadata (YAML + MD)
├── profile.md                # Existing: Living project profile
├── tasks/                    # Existing: Individual task memories
│   ├── {task_id}.md
│   └── ...
├── digests/                  # Existing: Weekly task summaries
│   ├── week-2026-W13.md
│   └── ...
├── notes/                    # Existing: Auto-generated insight notes
│   └── {project_id}/
│       ├── architecture-*.md
│       └── ...
├── knowledge/                # NEW: Topic-organized knowledge base
│   ├── architecture.md
│   ├── api-and-endpoints.md
│   ├── deployment.md
│   ├── dependencies.md
│   ├── gotchas.md
│   ├── conventions.md
│   └── decisions.md
└── staging/                  # NEW: Extracted facts awaiting consolidation
    ├── {task_id}.json
    └── processed/            # Processed staging files (cleaned periodically)
        └── ...
```

### Supervisor Integration

The supervisor should be updated to:

1. **Check factsheet first** when answering metadata questions
2. **Create a task** if the answer isn't in the factsheet (instead of refusing)
3. **Use `project_knowledge` tool** for topic-based questions before falling back to semantic search

Update supervisor prompts to include:
```
When a user asks a factual question about a project (URLs, tech stack, 
deployment, etc.), first check the project's factsheet for a direct answer.
If the factsheet doesn't have the answer, check the knowledge base topics.
If neither has the answer, create a task to investigate and update the 
project's knowledge base — never refuse to help.
```

### Bootstrap Process

For existing projects with task history but no factsheet/knowledge base:

1. **One-time migration task** reads all existing task memories, digests, notes, and profile
2. **Generates initial factsheet** from database (`projects.repo_url`, etc.) + extracted knowledge
3. **Generates initial knowledge base** by categorizing existing notes and profile content
4. **Indexes new files** in the vector database

This can be triggered manually (`/consolidate --bootstrap`) or runs automatically on first daily consolidation when no factsheet exists.

### Configuration

New config fields under `memory:` in `config.yaml`:

```yaml
memory:
  # ... existing fields ...
  
  # Knowledge consolidation
  consolidation_enabled: false        # Master switch for consolidation
  fact_extraction_enabled: true       # Extract facts after each task
  consolidation_schedule: "0 3 * * *" # Daily consolidation cron
  deep_consolidation_schedule: "0 4 * * 0"  # Weekly deep consolidation
  consolidation_provider: ""          # LLM provider (defaults to revision_provider)
  consolidation_model: ""             # Model override
  index_knowledge: true               # Index knowledge/ in vector DB
  factsheet_in_context: true          # Include factsheet in agent context (Tier 0)
  knowledge_topics:                   # Topic files to maintain
    - architecture
    - api-and-endpoints
    - deployment
    - dependencies
    - gotchas
    - conventions
    - decisions
```

### Metrics and Observability

The consolidation system tracks:
- Facts extracted per task (average, by category)
- Knowledge base coverage (which topics have content vs. empty)
- Factsheet completeness (which fields are populated)
- Consolidation run duration and token usage
- Stale fact count (facts with only old sources)

Exposed via `memory_stats` tool enhancement and API endpoint.

## Summary of Changes by File

| File | Change Type | Description |
|------|-------------|-------------|
| `src/memory.py` | Extend | Add factsheet management, knowledge base, fact extraction, consolidation methods; `write_memory` raises `OSError` on I/O failure (not silent `None`) |
| `src/models.py` | Extend | Add `factsheet` field to `MemoryContext`, new `ExtractedFact` dataclass |
| `src/config.py` | Extend | Add consolidation config fields to `MemoryConfig` |
| `src/prompts/memory_consolidation.py` | New | Prompts for fact extraction, consolidation, knowledge base |
| `src/plugins/internal/memory.py` | Extend | Add `project_factsheet`, `project_knowledge`, `search_all_projects` tools |
| `src/plugins/services.py` | Extend | Add 13 method delegations to `MemoryServiceImpl` for factsheet, knowledge base, consolidation, and key-value memory ops |
| `src/orchestrator.py` | Extend | Call fact extraction after task completion, register consolidation hooks |
| `src/supervisor.py` | Extend | Update prompts to check factsheet first |
| `specs/memory-consolidation.md` | New | This spec |
| `tests/test_memory_consolidation.py` | New | Tests for consolidation logic |
