---
tags: [design, self-improvement, memory, orchestrator, observability]
---

# Self-Improvement Loop

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#4 the system improves with use, #5 reduce human effort)
**Related:** [[memory-scoping]], [[memory-plugin]], [[vault]], [[playbooks]], [[profiles]]

---

## 1. Overview

The system improves over time through a closed loop: agents reflect on their work,
distill insights into scoped memory, and future agents benefit from accumulated
knowledge. This is the core value proposition — a system that gets better at its
job with less human intervention over time.

---

## 2. The Loop

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

---

## 3. What Gets Distilled vs. What Stays Raw

| Source | Distilled Into | Example |
|---|---|---|
| Task records | Project & agent-type insights | "SQLAlchemy needs explicit `expire_on_commit=False` in async sessions" |
| LLM logs | Operational insights, cost patterns | "Tasks averaging >50k tokens usually need to be split" |
| Error patterns | Agent-type memory | "When pytest fails with import errors, check sys.path before venv" |
| Successful strategies | Agent-type memory | "For this codebase, running ruff before tests catches 80% of issues" |
| Project conventions | Project memory | "This project uses factory pattern for all model creation" |

---

## 4. Feedback Loop Integrity

To prevent knowledge drift:
- Memories have timestamps; aged memories are candidates for consolidation or removal
- Memories can be tagged `#verified` or `#provisional` to indicate confidence
- Humans can edit/delete memories in Obsidian, and the file watcher picks up changes
- The reflection playbook considers whether old insights are still valid based on
  recent evidence

---

## 5. Orchestrator Memory

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

**On task completion:** The task-outcome [[playbooks|playbook]] can update the
project README if significant project state changed (new feature completed, major
bug fixed). This triggers the file watcher → orchestrator re-reads → summary updated.

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

## 6. Memory Health & Observability

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

## 7. Open Questions

1. **Memory capacity management.** Retention/archival policy as collections grow.
   Age-based? Relevance decay? Human curation only? The reflection playbook handles
   some consolidation, but long-term growth needs a strategy.

2. **Memory conflicts between agents.** Two agents might write contradictory insights.
   The deduplication merge handles similarity, but outright contradictions need a
   resolution strategy. Timestamp-wins? Tag as `#contested`? Surface for human review?

3. **Memory quality signal.** Measuring whether the self-improvement loop is actually
   helping. Track task success rates before/after memories are introduced? A/B test
   with and without memory retrieval?

4. **Privacy and multi-user.** If multiple users share an agent-queue instance, should
   memory be user-scoped or shared across all operators?
