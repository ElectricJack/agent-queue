# Vault & Memory Architecture

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable, #6 specificity wins, #8 components own dependencies)
**Related:** [[playbooks]], [[agent-coordination]], [[agent-profiles]], [[memory-consolidation]], [[plugin-system]]

---

## 1. Problem Statement

The current memory system is a single global vector store per project. Every agent
type — coding, code review, QA, orchestrator — searches the same pool of knowledge,
which is dominated by raw task records. This creates several problems:

**Cross-contamination.** A coding agent's hard-won insight about test patterns is
noise to a graphic design agent. A QA agent's memory about browser quirks is
irrelevant to a coding agent working on backend services. The more agent types we
add, the noisier retrieval becomes for all of them.

**No agent self-improvement.** There's no mechanism for an agent type to accumulate
wisdom from its own experience. A coding agent that resolves the same class of error
three times can't remember the pattern for next time. Insights exist in logs and
task records but never get distilled into reusable knowledge.

**Flat structure.** Memory is organized by project, but not by agent type, topic,
or specificity. There's no layering — no way to say "coding agents generally prefer
X, but on this project prefer Y instead."

**Opaque to humans.** The vector DB is a black box. Users can't browse, edit, curate,
or visualize the system's accumulated knowledge. There's no way to see what an agent
"knows" or correct misconceptions without querying the system.

**External knowledge isolation.** Project specs and docs live in their repos (rightly
so), but agents need to reason about them alongside their own memories. Currently,
workspace indexing exists but there's no curated bridge between repo docs and vault
knowledge.

---

## 2. Vision

The vault is a **structured, human-readable knowledge base** that serves as the single
source of truth for all editable system configuration and accumulated intelligence.
It is organized as an Obsidian-compatible folder that humans can browse, edit, and
visualize alongside the system's own read/write operations.

Memory is **scoped and layered** — each agent type accumulates its own wisdom, each
project has its own knowledge, and specificity trumps generality. Cross-cutting
insights are linked through Obsidian-style tags and references, not duplicated.

Agents improve over time by distilling insights from their work into their type's
memory, building a growing body of knowledge that makes each subsequent task more
informed. The system's goal is to get better at its job with less human intervention
over time.

---

## 3. Plugin Architecture

The memory system is implemented as a **self-contained internal plugin** that replaces
the current `memory.py` MemoryManager (2958 lines) and memory plugin facade (1046
lines) with a unified v2 plugin.

### Current Architecture (Being Replaced)

```
MemoryPlugin (thin facade, 1046 lines)
    → MemoryService (protocol wrapper)
        → MemoryManager (implementation, 2958 lines)
            → memsearch (external, Milvus vector DB)
            → filesystem (markdown files)
```

Three layers, two files, scattered responsibilities. The MemoryManager owns
profiles, factsheets, knowledge topics, consolidation, compaction, and indexing.
The plugin just translates tool calls.

### New Architecture

```
MemoryPlugin v2 (self-contained internal plugin)
    → memsearch fork (unified backend)
        → Milvus (single .db file or server)
            ├── Vector fields (semantic search)
            ├── Scalar fields (KV lookups via query/get)
            └── Metadata fields (tags, timestamps, scope)
    → Vault filesystem (human-readable source of truth)
```

One plugin. One external dependency. One storage backend. The plugin is the single
gateway for all memory operations — agents, playbooks, and the orchestrator all go
through the same interface.

### Why a Plugin (Not Core)

- **Self-contained.** The plugin brings its own storage (Milvus via memsearch fork).
  No dependency on the host system's database. Plugins should never require the core
  infrastructure's database — that creates tight coupling.
- **Replaceable.** A different memory implementation could swap in as a plugin without
  touching core code.
- **Plugin architecture stress test.** This exercises the plugin system's ability to
  handle a complex internal subsystem, validating the architecture for future
  sophisticated plugins.
- **Clean boundary.** The core system doesn't need to know how memory works. It calls
  `ctx.get_service("memory")` and gets results.

### memsearch Fork

The upstream [memsearch](https://github.com/zilliztech/memsearch) package provides
vector search over markdown files using Milvus. Our fork extends it with:

1. **Key-value storage.** Milvus already supports pure scalar queries via
   `query(filter='key == "test_command"')` and primary key lookups via `get(ids=[...])`.
   The fork adds a KV-oriented collection schema and convenience methods.
2. **Multi-collection search.** Query multiple collections in parallel with weighted
   result merging for scope-based retrieval.
3. **Scope-aware operations.** Collections are named by scope (`aq_system`,
   `aq_agenttype_coding`, `aq_project_mechfighters`). The fork handles scope
   resolution and routing.
4. **Metadata indexing.** Tags, timestamps, retrieval counts, and source tracking
   as scalar fields alongside vectors.

Milvus is a hybrid database — it natively supports vector similarity search AND
scalar field queries in the same collection. Key-value lookups use `query()` with
filter expressions, no vector computation needed. This means both semantic search
and exact KV retrieval go through one backend with zero additional infrastructure.

### KV Storage in Milvus

Each scope's collection includes entries that are pure key-value (no vector):

```python
# KV schema within each Milvus collection
kv_fields = [
    FieldSchema("entry_id", DataType.VARCHAR, is_primary=True),
    FieldSchema("entry_type", DataType.VARCHAR),   # "document" | "kv"
    FieldSchema("kv_namespace", DataType.VARCHAR),  # "project", "conventions", "stats"
    FieldSchema("kv_key", DataType.VARCHAR),
    FieldSchema("kv_value", DataType.VARCHAR),      # JSON-encoded
    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=768),  # null/zero for KV entries
    FieldSchema("content", DataType.VARCHAR),
    FieldSchema("source", DataType.VARCHAR),
    FieldSchema("tags", DataType.VARCHAR),           # JSON array
    FieldSchema("updated_at", DataType.INT64),
]
```

KV entries have `entry_type = "kv"` and are queried via scalar filters:

```python
# Exact KV lookup — no vector search, pure scalar query
results = collection.query(
    filter='entry_type == "kv" AND kv_namespace == "project" AND kv_key == "test_command"',
    output_fields=["kv_value"]
)

# List all KV entries in a namespace
results = collection.query(
    filter='entry_type == "kv" AND kv_namespace == "conventions"',
    output_fields=["kv_key", "kv_value"]
)
```

Document entries (memory files with embeddings) have `entry_type = "document"` and
are queried via vector similarity as before.

---

## 4. Vault Structure

The vault lives at `~/.agent-queue/vault/` and is the root of an Obsidian vault.

```
~/.agent-queue/
├── vault/                              # Obsidian vault root
│   ├── .obsidian/                      # Obsidian config (themes, plugins, etc.)
│   │
│   ├── system/
│   │   ├── playbooks/                  # System-scoped playbooks
│   │   │   ├── task-outcome.md
│   │   │   └── system-health.md
│   │   └── memory/                     # System-wide knowledge
│   │       └── global-conventions.md
│   │
│   ├── orchestrator/
│   │   ├── profile.md                  # Orchestrator profile definition
│   │   ├── playbooks/                  # Orchestrator-specific playbooks
│   │   └── memory/                     # Orchestrator's project understanding
│   │       ├── project-mech-fighters.md
│   │       └── project-agent-queue.md
│   │
│   ├── agent-types/
│   │   ├── coding/
│   │   │   ├── profile.md              # Tools, MCP servers, system prompt
│   │   │   ├── playbooks/              # Agent-type playbooks
│   │   │   └── memory/                 # Cross-project coding wisdom
│   │   │       ├── async-patterns.md
│   │   │       └── migration-gotchas.md
│   │   ├── code-review/
│   │   │   ├── profile.md
│   │   │   ├── playbooks/
│   │   │   └── memory/
│   │   └── qa/
│   │       ├── profile.md
│   │       ├── playbooks/
│   │       └── memory/
│   │
│   ├── projects/
│   │   ├── mech-fighters/
│   │   │   ├── README.md               # Project overview & context
│   │   │   ├── memory/                 # Project-specific knowledge
│   │   │   │   ├── knowledge/          # Topic-based knowledge files
│   │   │   │   └── insights/           # Distilled insights from tasks/logs
│   │   │   ├── playbooks/              # Project-scoped playbooks
│   │   │   ├── notes/                  # Human-authored project notes
│   │   │   ├── references/             # Auto-generated spec/doc stubs
│   │   │   │   ├── spec-orchestrator.md
│   │   │   │   └── spec-database.md
│   │   │   └── overrides/              # Per-project agent-type tweaks
│   │   │       ├── coding.md
│   │   │       └── qa.md
│   │   └── agent-queue/
│   │       └── ...
│   │
│   └── templates/                      # Templates for new profiles, playbooks
│       ├── profile-template.md
│       └── playbook-template.md
│
├── config.yaml                         # System configuration
├── .env                                # Secrets
├── memsearch/                          # Milvus storage (vectors + KV)
│   └── milvus.db                       # Milvus Lite file (or server URI)
├── compiled/                           # Compiled playbook JSON (runtime)
│   ├── task-outcome.compiled.json
│   └── code-quality-gate.compiled.json
├── tasks/                              # Task records (not in vault)
│   ├── mech-fighters/
│   │   └── {task_id}.md
│   └── agent-queue/
│       └── {task_id}.md
├── logs/                               # Logs (not in vault)
│   ├── agent-queue.log
│   └── llm/
├── plugins/                            # Plugin installations
└── plugin-data/                        # Plugin runtime data
```

### What Lives in the Vault vs. Outside

| In the vault | Outside the vault |
|---|---|
| Playbook markdown (source of truth) | Compiled playbook JSON (runtime artifact) |
| Agent profiles (source of truth) | PostgreSQL (runtime sync target for profiles) |
| Curated memory & insights | Raw task records |
| Fact files (KV source of truth) | Milvus storage (vectors + KV index) |
| Project notes & overrides | Logs |
| Reference stubs for external docs | Config, secrets, plugins |
| System-wide knowledge | |

The principle: **the vault contains what humans and agents author and curate.** Everything
else — runtime artifacts, raw data, infrastructure — lives outside.

Note: I like [[memory-consolidation#`project_factsheet`]] @TODO - lets figure out how to incorporate the existing project fact-sheet functionality 

---

## 4. Memory Scoping & Layering

Memory is organized into **scopes** that form a specificity hierarchy. More specific
scopes override or supplement broader ones.

### Scope Hierarchy (Broadest → Most Specific)

```
system
  └── agent-type (e.g., coding)
       └── project (e.g., mech-fighters)
            └── project + agent-type override (e.g., mech-fighters/overrides/coding.md)
```

### How Scopes Compose

When a coding agent working on mech-fighters searches memory, the system queries
multiple vector collections and merges results weighted by specificity:

1. **Project override** (`projects/mech-fighters/overrides/coding.md`) — highest weight
2. **Project memory** (`projects/mech-fighters/memory/`) — project-specific knowledge
3. **Agent-type memory** (`agent-types/coding/memory/`) — cross-project coding wisdom
4. **System memory** (`system/memory/`) — lowest weight, broadest knowledge

Results are interleaved by relevance score adjusted for scope weight. A moderately
relevant project-specific memory outranks a highly relevant system memory.

### Override Model

Overrides are **freeform English** that supplement or tweak the parent agent-type
profile for a specific project. They are not structured config — an LLM interprets
them as contextual guidance.

Example `projects/mech-fighters/overrides/coding.md`:

```markdown
---
tags: [override, coding, mech-fighters]
agent_type: coding
---

# Coding Agent Overrides — Mech Fighters

This project uses a custom ECS framework. Do not use inheritance for
game entities — always use composition via the component system.

Prefer integration tests that spin up the full game loop over unit
tests of individual components. The component system has too many
implicit interactions for isolated unit tests to catch real bugs.

The project has a custom asset pipeline — never modify files in
assets/generated/ directly. Always edit the source files in
assets/source/ and run the pipeline.
```

The override is injected into the agent's context alongside its base profile. The
LLM resolves any tension between the base profile and the override naturally, with
the override taking precedence as the more specific guidance.

---

## 6. Milvus Backend Topology

### One Collection per Memory Scope

Each scope maps to a single Milvus collection containing both document entries
(with embeddings for semantic search) and KV entries (scalar-only for exact lookup):

| Scope | Collection Name | Indexes |
|---|---|---|
| System | `aq_system` | `vault/system/memory/`, `vault/system/facts.md` |
| Orchestrator | `aq_orchestrator` | `vault/orchestrator/memory/`, `vault/orchestrator/facts.md` |
| Agent type | `aq_agenttype_{type}` | `vault/agent-types/{type}/memory/`, `vault/agent-types/{type}/facts.md` |
| Project | `aq_project_{id}` | `vault/projects/{id}/memory/`, `vault/projects/{id}/notes/`, `vault/projects/{id}/references/`, `vault/projects/{id}/facts.md` |

Project collections also index **workspace specs and docs** directly from the project
repo (not duplicated into the vault). This is the existing workspace indexing behavior,
retained.

### Fact Files (KV Source of Truth)

Each scope can have a `facts.md` in the vault — a human-readable file of structured
key-value data that gets synced to Milvus KV entries:

```markdown
---
tags: [facts, auto-updated]
---

# Project Facts — Mech Fighters

## Project
- tech_stack: [Python 3.12, SQLAlchemy, Pygame]
- deploy_branch: main
- test_command: pytest tests/ -v
- repo_url: github.com/user/mech-fighters

## Conventions
- orm_pattern: repository
- naming: snake_case

## Stats
- total_tasks_completed: 47
- avg_task_tokens: 32000
```

The format is `key: value` pairs under markdown headings (namespaces). Parsed
deterministically (no LLM needed). The file watcher detects changes and syncs
to Milvus KV entries. Agents can also write KV entries via MCP tools, which
update both the Milvus collection and the vault fact file.

### Multi-Scope Query

The plugin queries all relevant collections in parallel and merges results. This
applies to both semantic search and KV lookups:

```python
async def search(
    query: str,
    agent_type: str,
    project_id: str,
    limit: int = 10,
) -> list[MemoryResult]:
    """Semantic search across all relevant scopes, weighted by specificity."""
    results = await asyncio.gather(
        self._search_collection(f"aq_project_{project_id}", query, weight=1.0),
        self._search_collection(f"aq_agenttype_{agent_type}", query, weight=0.7),
        self._search_collection(f"aq_system", query, weight=0.4),
    )
    return merge_and_rank(results, limit=limit)

async def recall(
    key: str,
    agent_type: str,
    project_id: str,
    namespace: str | None = None,
) -> str | None:
    """KV lookup with scope resolution. First match wins (most specific)."""
    for scope in [f"aq_project_{project_id}", f"aq_agenttype_{agent_type}", "aq_system"]:
        result = self._kv_get(scope, key, namespace)
        if result is not None:
            return result
    return None
```

KV lookups follow **first-match-wins** (most specific scope first). Semantic search
uses **weighted merging** (all scopes contribute, specificity boosts ranking).

Override files (`overrides/coding.md`) are indexed into the project collection. They
are found by project-scope search and naturally weighted highest.

### Tag-Based Cross-Scope Discovery

Standard search is scoped. But some queries need to cross boundaries — "what
do we know about SQLite across all projects and agent types?"

Tags are stored as scalar fields in Milvus. A secondary search mode queries **all
collections** filtered by tag:

```python
async def search_by_tag(
    tag: str,
    limit: int = 10,
) -> list[MemoryResult]:
    """Search across ALL collections for memories with a specific tag."""
    # Uses Milvus scalar filter: tags LIKE '%"sqlite"%'
    ...
```

This is the mechanism for cross-cutting discovery. The Obsidian graph view renders
the same connections visually.

---

## 7. Profiles as Markdown

Agent profiles are **markdown files in the vault** that serve as the source of truth
for agent-type configuration. The database stores a synced copy for fast runtime
access.

### Profile File Structure — Hybrid Format

Profiles use a **hybrid approach**: freeform English for guidance that gets injected
into the agent's prompt, and JSON code blocks for structured configuration that
requires exact parsing. This avoids the fragility of LLM-parsing tool configurations
while keeping behavioral guidance human-readable.

`vault/agent-types/coding/profile.md`:

````markdown
---
id: coding
name: Coding Agent
tags: [profile, agent-type]
---

# Coding Agent

## Role
You are a software engineering agent. You write, modify, and debug code
within a project workspace. You follow project conventions, write tests,
and commit clean, working code.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto",
  "max_tokens_per_task": 100000
}
```

## Tools
```json
{
  "allowed": ["shell", "file_read", "file_write", "git", "vibecop_scan", "vibecop_check"],
  "denied": []
}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  }
}
```

## Rules
- Always run existing tests before committing
- Never commit secrets, .env files, or credentials
- Prefer small, focused commits over large ones
- If tests fail after your changes, fix them before moving on
- Check for and respect any project-specific overrides

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
- Did I resolve an error that might recur? If so, save the pattern.
- Is there a convention in this project I should note for next time?
````

**What gets parsed deterministically (JSON blocks):**
- `## Config` → model, permission mode, token limits → DB fields
- `## Tools` → allowed/denied tool lists → DB fields
- `## MCP Servers` → exact server configurations → DB fields

**What gets injected as prompt context (English sections):**
- `## Role` → system prompt prefix
- `## Rules` → behavioral guidance in agent context
- `## Reflection` → post-task reflection instructions

This split means misconfigured MCP servers are caught by JSON parse errors (not
LLM misinterpretation), while behavioral guidance stays natural and editable.

### Sync Model

```
Human edits profile.md in Obsidian
       │                              Agent updates profile via chat command
       │                                       │
       ▼                                       ▼
  File watcher detects change          System writes to profile.md
       │                                       │
       ▼                                       ▼
  Parse profile ◄─────────────────────────────┘
       │
       ├── Extract JSON code blocks → validate → structured DB fields
       ├── Extract English sections → store as prompt text
       │
       ▼
  Update DB row (agent_profiles table)
       │
       ▼
  Runtime picks up new config
```

All writes flow through the markdown file. The chat/dashboard interface writes to
the file, not the DB. The file watcher handles sync in one direction only:
**markdown → DB**. No bidirectional sync, no conflicts.

**Validation on sync:**
- JSON blocks must parse successfully; if not, sync fails and the previous DB
  config remains active. An error notification is sent.
- Tool names in `## Tools` are validated against the tool registry. Unknown tools
  produce a warning (not a hard failure — the tool may not be loaded yet).
- MCP server commands are validated for basic structure (command exists, args are
  strings). Server health is not checked at sync time.

### Starter Knowledge Packs

New agent types start with no memory. To avoid a cold-start problem, the system
ships **starter knowledge packs** in `vault/templates/knowledge/`:

```
vault/templates/knowledge/
  coding/
    common-pitfalls.md           # "Always check for async/sync mismatches..."
    git-conventions.md           # "Prefer small commits, meaningful messages..."
  code-review/
    review-checklist.md          # "Check for: error handling, edge cases..."
  qa/
    testing-patterns.md          # "Prefer integration tests for critical paths..."
```

When a new agent type is created (profile.md saved for the first time), the system
copies matching starter knowledge from `templates/knowledge/{type}/` to the agent
type's `memory/` folder if one exists. These starter files are tagged `#starter`
and can be updated or removed as the agent accumulates real experience.

---

## 8. Reference Stubs for External Docs

Project specs and documentation live in their repos and should stay there. The vault
contains **reference stubs** — lightweight summaries that bridge the gap.

### Generation

A background indexer (not a full playbook — too mechanical for LLM orchestration)
monitors project workspaces for spec/doc changes and generates stubs:

1. Detect spec/doc file change in workspace (via file watcher or git diff)
2. Read the full document
3. Generate a summary with key decisions, interfaces, and concepts extracted
4. Write to `vault/projects/{id}/references/spec-{name}.md`
5. Index the stub into the project's vector collection

### Stub Format

```markdown
---
tags: [spec, reference, auto-generated]
source: /path/to/project/specs/orchestrator.md
source_hash: abc123
last_synced: 2026-04-07
---

# Spec: Orchestrator

Full spec at `specs/orchestrator.md` in the mech-fighters workspace.

## Summary
Defines the main orchestration loop including task assignment,
agent lifecycle management, and the tick-based execution model.

## Key Decisions
- Tick-based loop at ~5s interval
- Single agent per task, no sharing
- Tasks assigned by priority then age

## Key Interfaces
- `Orchestrator.tick()` — main loop entry
- `Orchestrator.assign_task()` — agent selection
- EventBus integration for all state changes
```

### Why Stubs, Not Symlinks

- Symlinks break across WSL/Windows boundaries and in Obsidian
- Stubs are **better for retrieval** — a distilled summary with key decisions
  extracted searches more effectively than a 500-line raw spec
- Stubs can include vault-native tags and wikilinks that raw specs can't
- The full spec is still indexed directly by the vector DB for deep queries

---

## 9. Agent Memory Write Path

Agents write to memory through MCP tools, not direct file access. This ensures
proper indexing, deduplication, and file placement.

### MCP Tools for Memory

**Semantic (unstructured knowledge):**

| Tool | Description |
|---|---|
| `memory_search` | Semantic search across relevant scopes (vector similarity) |
| `memory_save` | Save an insight/learning as a memory file (with dedup) |
| `memory_list` | Browse memories in a scope |

**Key-value (structured facts):**

| Tool | Description |
|---|---|
| `memory_recall` | Exact KV lookup by key, with scope resolution (most specific wins) |
| `memory_store` | Store a key-value pair in the appropriate scope |
| `memory_list_facts` | List all KV entries in a scope/namespace |

**Unified (auto-routing):**

| Tool | Description |
|---|---|
| `memory_get` | Smart retrieval: tries KV exact match first, falls back to semantic search. Agents use this when they're not sure which retrieval strategy is appropriate |

The `memory_` prefix keeps all tools grouped and discoverable. Agents can use the
specific tools when they know what they want, or `memory_get` when they don't.

### `memory_save` Flow

```
Agent calls memory_save(content, tags)
       │
       ▼
  MemoryManager determines scope:
    - Agent type → vault/agent-types/{type}/memory/
    - Project (if specified) → vault/projects/{id}/memory/insights/
       │
       ▼
  Check for duplicates (semantic similarity search in target scope)
       │
       ├── Similarity > 0.95: Near-identical. Update timestamp on existing
       │   memory, append source task reference. No content change needed.
       │
       ├── Similarity 0.8–0.95: Related. Invoke LLM to merge:
       │   - Provide both old and new content
       │   - Instructions: "Combine these into a single memory. If they
       │     contradict, prefer the newer information but note the change.
       │     Preserve tags from both."
       │   - Write merged content back to existing file
       │   - Update vector embedding for the merged version
       │
       ├── Similarity < 0.8: Distinct. Create new file.
       │
       ▼
  Write/update markdown file with frontmatter (tags, timestamp, source task)
       │
       ▼
  Index into appropriate vector collection
       │
       ▼
  Return confirmation to agent with action taken (created/merged/deduplicated)
```

The merge LLM call is lightweight — it's combining two short documents, not
reasoning about a complex problem. It uses the same model as transition evaluation
(cheap/fast).

### Reflection Playbook (Periodic Consolidation)

Agents write insights immediately during task execution. A separate
**[[playbooks|reflection playbook]]** runs periodically to:

1. Review recent task records and logs for the agent type
2. Extract patterns the agent didn't catch in real-time
3. Consolidate overlapping memories (merge duplicates, update outdated ones)
4. Promote insights that apply broadly (re-tag for wider scope)
5. Archive memories that are no longer relevant

This is the "step back and think" complement to the "capture in the moment" writes.
The combination ensures both immediate learning and systematic knowledge management.

---

## 10. Orchestrator Memory

The orchestrator is its own agent type with its own memory scope. It maintains a
**high-level understanding of each project** rather than searching all project
collections directly.

### How the Orchestrator Learns About Projects

The orchestrator's project understanding is maintained through three mechanisms:

**On startup:** The orchestrator scans all `vault/projects/*/README.md` files and
generates/updates summaries in `vault/orchestrator/memory/project-{id}.md`.

**On README change:** A file watcher monitors `vault/projects/*/README.md`. When a
README changes, the orchestrator re-reads it and updates its summary. This is a
lightweight indexer operation (similar to reference stub generation), not a full
playbook.

**On task completion:** The task-outcome playbook can update the project README if
significant project state changed (new feature completed, major bug fixed). This
triggers the file watcher → orchestrator re-reads → summary updated.

Each project summary captures: project purpose, tech stack, current state, key
concerns, active work areas, and which agent types have been effective. The
orchestrator uses this understanding when [[agent-coordination|coordination
playbooks]] ask it to assign agents to workflows.

### What the Orchestrator Remembers

- Project summaries (synthesized from READMEs)
- Task assignment patterns (which agent types work well for which kinds of tasks)
- Scheduling insights (what times/orders produce better results)
- System-level operational knowledge

The orchestrator does **not** search individual project memory collections during
normal operation. It works from its own distilled understanding. If it needs project
details, it reads the project README or delegates to a project-aware agent.

---

## 11. The Self-Improvement Loop

The system improves over time through a closed loop:

```
Task Execution
    │
    ├── Agent writes immediate insights via MCP (memory_save)
    ├── Agent stores structured facts via MCP (memory_store)
    │
    ▼
Task Completion
    │
    ├── Task record saved (outside vault)
    │
    ▼
Reflection Playbook (periodic)
    │
    ├── Reviews recent task records & logs
    ├── Extracts patterns, recurring errors, successful strategies
    ├── Writes distilled insights to agent-type memory
    ├── Consolidates & deduplicates existing memories
    │
    ▼
Log Analysis Playbook (periodic)
    │
    ├── Scans recent system logs for anomalies
    ├── Identifies error patterns, performance issues, resource waste
    ├── Writes operational insights to orchestrator memory
    │
    ▼
Next Task Execution
    │
    ├── Agent's memory search returns insights from prior work
    ├── Agent performs better, avoids known pitfalls
    │
    ▼
  (cycle continues)
```

### What Gets Distilled vs. What Stays Raw

| Source | Distilled Into | Example |
|---|---|---|
| Task records | Project & agent-type insights | "SQLAlchemy needs explicit `expire_on_commit=False` in async sessions" |
| LLM logs | Operational insights, cost patterns | "Tasks averaging >50k tokens usually need to be split" |
| Error patterns | Agent-type memory | "When pytest fails with import errors, check sys.path before venv" |
| Successful strategies | Agent-type memory | "For this codebase, running ruff before tests catches 80% of issues" |
| Project conventions | Project memory | "This project uses factory pattern for all model creation" |

### Feedback Loop Integrity

To prevent knowledge drift:
- Memories have timestamps; aged memories are candidates for consolidation or removal
- Memories can be tagged `#verified` or `#provisional` to indicate confidence
- Humans can edit/delete memories in Obsidian, and the file watcher picks up changes
- The reflection playbook considers whether old insights are still valid based on
  recent evidence

---

## 12. Obsidian Integration

The vault is designed to be a first-class Obsidian experience.

### Graph View

Obsidian's graph view renders relationships between vault files based on wikilinks
and tags. The vault structure produces a natural graph:

- **Agent types** cluster around their memories and playbooks
- **Projects** cluster around their notes, references, and overrides
- **Cross-cutting tags** (`#sqlite`, `#security`, `#performance`) create bridges
  between otherwise separate clusters
- **Reference stubs** link projects to their spec summaries
- **Override files** link projects to agent types

### Tagging Conventions

| Tag | Meaning |
|---|---|
| `#insight` | Distilled learning from experience |
| `#override` | Project-specific agent-type tweak |
| `#reference` | Auto-generated stub for external doc |
| `#auto-generated` | Created by the system, not a human |
| `#verified` | Human-reviewed and confirmed accurate |
| `#provisional` | System-generated, not yet verified |
| `#profile` | Agent type profile definition |
| `#playbook` | Workflow graph definition |
| Domain tags | `#sqlite`, `#async`, `#security`, etc. |

### Wikilink Conventions

- `[[agent-types/coding/profile]]` — link to an agent profile
- `[[projects/mech-fighters/README]]` — link to a project
- `[[system/playbooks/task-outcome]]` — link to a playbook

These conventions are guidelines, not enforced structure. The LLM and human authors
use them naturally; the system doesn't parse wikilinks programmatically.

---

## 13. Memory Health & Observability

The self-improvement loop needs visibility to know if it's working.

### Memory Health View

A command/dashboard view that surfaces:

| Metric | Description |
|---|---|
| Collection sizes | File count and vector count per scope |
| Most-retrieved memories | Which memories are actually surfaced in agent context |
| Stale memories | Not retrieved in N days — candidates for archival |
| Memory growth rate | New memories per agent type per week |
| Retrieval hit rate | How often search returns results agents actually use |
| Contradictions | Memories tagged `#contested` or with merge conflicts |

### Memory Audit Trail

Each memory file tracks its lineage in frontmatter:

```yaml
created: 2026-04-07
source_task: task-abc123
source_playbook: task-outcome
last_retrieved: 2026-04-15
retrieval_count: 7
```

`retrieval_count` and `last_retrieved` are updated by the MemoryManager when search
results are returned. This powers the staleness and hit-rate metrics.

---

## 14. Migration Path

### Phase 1: Vault Structure + Task Migration

- Create `~/.agent-queue/vault/` with the directory structure
- Move existing `.obsidian/` config from `memory/` to `vault/`
- **Move task records** from `memory/*/tasks/` to `tasks/*/` (outside vault) — stops
  task records from polluting memory search results
- Move existing rule files from `memory/*/rules/` to `vault/` playbook locations
- Move existing notes from `notes/` to `vault/projects/*/notes/`
- Symlink or copy existing project memory files during transition
- Implement the [[playbooks#17. Prerequisite Refactors|unified vault file watcher]]

### Phase 2: memsearch Fork + Plugin v2

- Fork memsearch, add KV storage (scalar-only entries in Milvus collections)
- Add multi-collection query with weighted merging
- Add scope-aware collection naming and routing
- Build MemoryPlugin v2 as internal plugin using the fork
- Plugin exposes unified tool set (`memory_search`, `memory_recall`, `memory_save`,
  `memory_store`, `memory_get`, `memory_list`, `memory_list_facts`)
- v2 plugin coexists with v1 during transition; both register via plugin system
- Migrate existing per-project collections to scoped collections

### Phase 3: Memory Scoping + KV

- Create per-agent-type and system-level collections
- Implement scope resolution (project → agent-type → system) for both search and KV
- Create fact files (`facts.md`) per scope in the vault
- Implement fact file → Milvus KV sync via file watcher
- Remove v1 memory plugin and `src/memory.py` MemoryManager

### Phase 4: Profile Migration

- Convert DB-stored profiles to markdown files in the vault
- Implement file watcher → DB sync for hybrid profile format
- Update chat/dashboard profile commands to write markdown
- Validate JSON block parsing and DB sync

### Phase 5: Self-Improvement Loop

- Implement reflection playbook for agent-type insight extraction
- Implement log analysis playbook for operational insights
- Implement reference stub indexer for workspace specs
- Implement memory consolidation (dedup, merge, archive)
- Implement memory health metrics and audit trail

---

## 15. Resolved Design Decisions

These were originally open questions, now resolved:

- **Profile parsing safety:** Hybrid markdown format — JSON code blocks for structured
  config (tools, MCP, model), freeform English for behavioral guidance (Section 7)
- **Memory deduplication:** Three-tier similarity thresholds with LLM-assisted merge
  for related but not identical memories (Section 9)
- **Orchestrator project triggers:** File watcher on project READMEs + startup scan +
  indirect updates via task-outcome playbook (Section 10)
- **Cold start / bootstrap:** Starter knowledge packs copied from templates on first
  agent-type creation (Section 7)
- **KV storage backend:** Milvus scalar fields, not PostgreSQL. The memory plugin is
  self-contained with no dependency on the core database (Section 3)
- **Plugin architecture:** v2 internal plugin replaces both `src/memory.py` and the
  current memory plugin facade. memsearch fork as the unified backend (Section 3)
- **Agent tool naming:** `memory_` prefix for all tools. Specific tools for KV
  (`memory_recall`, `memory_store`) and semantic (`memory_search`, `memory_save`),
  plus unified `memory_get` for auto-routing (Section 9)

---

## 16. Open Questions

1. **Memory capacity management.** As agent types accumulate memories over months,
   collections will grow. What's the retention/archival policy? Age-based? Relevance
   decay? Human curation only? The reflection playbook handles some consolidation,
   but long-term growth needs a strategy.

2. **Memory conflicts between agents.** Two coding agents working on different tasks
   might write contradictory insights to the same type memory. The deduplication
   merge handles similarity, but outright contradictions (agent A says "always use
   approach X", agent B says "never use approach X") need a resolution strategy.
   Timestamp-wins? Tag as `#contested`? Surface for human review?

3. **Obsidian plugin opportunities.** Custom Obsidian plugins could surface live system
   state (running tasks, playbook instances) in the vault. How far do we take the
   Obsidian integration?

4. **Privacy and multi-user.** If multiple users share an agent-queue instance, should
   memory be user-scoped? Or is the system's knowledge shared across all operators?

5. **Memory quality signal.** How do we measure whether the self-improvement loop is
   actually helping? Track task success rates before/after memories are introduced?
   A/B test with and without memory retrieval?

6. **Embedding model consistency.** If the embedding model changes (e.g., upgrade from
   one provider to another), all collections need re-indexing. How do we handle this
   gracefully?

7. **Reference stub freshness.** The stub indexer monitors workspaces for spec/doc
   changes — but what triggers the monitoring? A file watcher on the workspace? A
   git hook? What if the workspace isn't mounted? Stubs with stale `source_hash`
   should be flagged but the detection mechanism needs defining.
