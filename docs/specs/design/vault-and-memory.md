---
tags: [design, vault, memory, overview]
---

# Vault & Memory Architecture — Overview

**Status:** Draft
**Principles:** [[guiding-design-principles]]

This is the hub document for the vault and memory system redesign. The full
design is split across focused specs:

| Spec | What it covers |
|---|---|
| [[vault]] | Vault directory structure, what lives where, reference stubs, Obsidian integration |
| [[memory-plugin]] | Plugin v2 architecture, memsearch fork, Milvus backend, KV storage |
| [[memory-scoping]] | Scope hierarchy, overrides, multi-scope query, agent MCP tools |
| [[profiles]] | Agent profiles as markdown, hybrid format, sync model, starter packs |
| [[self-improvement]] | Self-improvement loop, orchestrator memory, health & observability |

Related design docs: [[playbooks]], [[agent-coordination]], [[guiding-design-principles]]

---

## Problem Statement

The current memory system is a single global vector store per project. Every agent
type searches the same pool of knowledge, which is dominated by raw task records.
This creates cross-contamination, no agent self-improvement path, flat structure
with no scoping, opacity to humans, and isolation from external project docs.

See individual specs above for how each aspect is addressed.

---

## Migration Path

### Phase 1: Vault Structure + Task Migration

- Create `~/.agent-queue/vault/` with the [[vault|directory structure]]
- Move existing `.obsidian/` config from `memory/` to `vault/`
- **Move task records** from `memory/*/tasks/` to `tasks/*/` (outside vault)
- Move existing rule files from `memory/*/rules/` to vault playbook locations
- Move existing notes from `notes/` to `vault/projects/*/notes/`
- Symlink or copy existing project memory files during transition
- Implement the [[playbooks#17. Prerequisite Refactors|unified vault file watcher]]

### Phase 2: memsearch Fork + Plugin v2

- Fork memsearch, add KV storage (scalar-only entries in Milvus collections)
- Add multi-collection query with weighted merging
- Add scope-aware collection naming and routing
- Build [[memory-plugin|MemoryPlugin v2]] as internal plugin using the fork
- Plugin exposes unified tool set (`memory_search`, `memory_recall`, `memory_save`,
  `memory_store`, `memory_get`, `memory_list`, `memory_list_facts`)
- v2 plugin coexists with v1 during transition; both register via plugin system
- Migrate existing per-project collections to scoped collections

### Phase 3: Memory Scoping + KV

- Create per-agent-type and system-level collections
- Implement [[memory-scoping|scope resolution]] (project → agent-type → system)
- Create fact files (`facts.md`) per scope in the vault
- Implement fact file → Milvus KV sync via file watcher
- Remove v1 memory plugin and `src/memory.py` MemoryManager

### Phase 4: Profile Migration

- Convert DB-stored profiles to [[profiles|markdown files]] in the vault
- Implement file watcher → DB sync for hybrid profile format
- Update chat/dashboard profile commands to write markdown
- Validate JSON block parsing and DB sync

### Phase 5: Self-Improvement Loop

- Implement [[self-improvement|reflection playbook]] for agent-type insight extraction
- Implement log analysis playbook for operational insights
- Implement reference stub indexer for workspace specs
- Implement memory consolidation (dedup, merge, archive)
- Implement memory health metrics and audit trail

---

## Resolved Design Decisions

- **Profile parsing safety:** Hybrid markdown format — JSON code blocks for structured
  config, freeform English for behavioral guidance ([[profiles]])
- **Memory deduplication:** Three-tier similarity thresholds with LLM-assisted merge
  ([[memory-scoping]])
- **Orchestrator project triggers:** File watcher on project READMEs + startup scan
  ([[self-improvement]])
- **Cold start / bootstrap:** Starter knowledge packs ([[profiles]])
- **KV storage backend:** Milvus scalar fields, not PostgreSQL ([[memory-plugin]])
- **Plugin architecture:** v2 internal plugin, memsearch fork as unified backend
  ([[memory-plugin]])
- **Agent tool naming:** `memory_` prefix for all tools ([[memory-scoping]])

---

## Open Questions

1. **Memory capacity management.** Retention/archival policy as collections grow.
   Age-based? Relevance decay? Human curation only?

2. **Memory conflicts between agents.** Contradictory insights from different agents.
   Timestamp-wins? Tag as `#contested`? Surface for human review?

3. **Obsidian plugin opportunities.** How far to take the Obsidian integration —
   custom plugins for live system state?

4. **Privacy and multi-user.** Should memory be user-scoped or shared?

5. **Memory quality signal.** Measuring whether the [[self-improvement]] loop helps.

6. **Embedding model consistency.** Re-indexing strategy when the embedding model changes.

7. **Reference stub freshness.** Detection mechanism for stale [[vault|reference stubs]].
