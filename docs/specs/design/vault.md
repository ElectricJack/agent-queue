---
tags: [design, vault, obsidian, structure]
---

# Vault Structure & Obsidian Integration

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable)
**Related:** [[memory-plugin]], [[memory-scoping]], [[profiles]], [[self-improvement]], [[playbooks]]

---

## 1. Overview

The vault is a **structured, human-readable knowledge base** at `~/.agent-queue/vault/`
that serves as the single source of truth for all editable system configuration and
accumulated intelligence. It is organized as an Obsidian-compatible folder that humans
can browse, edit, and visualize alongside the system's own read/write operations.

---

## 2. Directory Layout

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

---

## 3. What Lives in the Vault vs. Outside

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

---

## 4. Reference Stubs for External Docs

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

## 5. Obsidian Integration

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
