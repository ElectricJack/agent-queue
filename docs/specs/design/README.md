---
tags: [design, overview, index]
---

# Next-Generation Design Specs

These specs describe the automation and knowledge systems of Agent Queue.
They are **design documents** that serve as the architectural reference for the current implementation.

## Documents

| Spec | Status | Summary |
|---|---|---|
| [[guiding-design-principles]] | Active | The 10 core principles behind all design decisions |
| [[playbooks]] | Active | Agent workflow graphs — directed graphs of LLM decision points, replaced rules + hooks |
| [[vault]] | Active | Vault directory structure, what lives where, reference stubs, Obsidian integration |
| [[memory-plugin]] | Active | Memory plugin v2 architecture, memsearch fork, Milvus backend with KV storage |
| [[memory-scoping]] | Active | Scope hierarchy, overrides, multi-scope query, agent MCP tools, deduplication |
| [[profiles]] | Active | Agent profiles as markdown, hybrid format, sync model, starter knowledge packs |
| [[self-improvement]] | Active | Self-improvement loop, orchestrator memory, reflection, health & observability |
| [[agent-coordination]] | Active | Playbook-driven multi-agent coordination, workflows, agent affinity, workspace strategies |
| [[roadmap]] | Active | 196-task implementation roadmap across 8 phases with dependencies and test checkpoints |

## How They Relate

```
guiding-design-principles
    ├── Referenced by all specs as the decision framework
    │
    ▼
vault + memory-plugin + memory-scoping          playbooks
    vault structure & Obsidian        ◄──►  playbooks live in the vault
    memory plugin & Milvus backend    ◄──►  playbooks read/write memory
    profiles (markdown)               ◄──►  agent-type scoped playbooks
    self-improvement loop             ◄──►  reflection playbooks drive the loop
              │                                    │
              │                                    │
              └──────────────┬─────────────────────┘
                             │
                             ▼
                     agent-coordination
                     uses playbook execution model
                     reads profiles from the vault
                     agent affinity + memory reduce context loss
```

All specs share the same [[guiding-design-principles|design principles]].
The [[vault]] is the prerequisite for [[playbooks|playbook]] storage.
[[playbooks|Playbooks]] are the mechanism for the
[[self-improvement|self-improvement loop]].
[[agent-coordination|Coordination playbooks]] extend the playbook model to
multi-agent workflows. [[memory-plugin]] and [[memory-scoping]] define how
all memory operations work.

### Prerequisite Refactors (in [[playbooks]] Section 17)

Several existing subsystems need changes before playbooks can be implemented:
- **EventBus payload filtering** — needed for cross-playbook composition
- **Event schema registry** — enforce event payload contracts
- **GitManager event emission** — `git.commit`, `git.push`, `git.pr.created`
- **Supervisor config flexibility** — per-call model/tool overrides
- **Unified vault file watcher** — one watcher for the whole vault
- **Task records migration** — move out of memory/ to stop polluting search

## End-to-End Trace

A concrete walkthrough showing [[playbooks]], [[memory-scoping]], and
[[agent-coordination]] working together:

**Scenario:** A coding agent commits code, vibecop finds issues, the system creates
fix tasks, and the experience is remembered for next time.

```
1. COMMIT EVENT
   Coding agent on mech-fighters commits changes to src/combat.py.
   GitManager emits: git.commit {project_id: "mech-fighters", agent_id: "agent-3",
     changed_files: ["src/combat.py"], commit_hash: "abc123"}

2. PLAYBOOK MATCH
   Executor matches event to code-quality-gate playbook (project-scoped,
   triggers: [git.commit]). Creates PlaybookRun record, status: running.

3. NODE: scan
   Executor creates Supervisor, seeds conversation with event JSON.
   Sends node prompt: "Run vibecop_check on the files changed in this commit..."
   Supervisor calls vibecop_check tool -> finds 2 errors, 1 warning.
   Response added to conversation history.

4. TRANSITION: scan -> triage
   Separate LLM call: "Based on the result above, which condition is met?
   - no findings / - findings exist"
   Result: "findings exist" -> goto triage

5. NODE: triage
   Executor sends triage prompt with full conversation history (includes
   scan results). LLM groups: 2 errors in combat.py, 1 warning in combat.py.

6. TRANSITION: triage -> create_error_tasks
   "has errors" matches -> goto create_error_tasks

7. NODE: create_error_tasks
   LLM creates a high-priority task. Since agent-3 is still running, the task
   is attached as a follow-up. Supervisor uses create_task tool.
   LLM also calls memory_save via MCP:
     "vibecop frequently catches unhandled None checks in combat systems"
     tags: [vibecop, combat, coding-pattern]
     -> Written to vault/agent-types/coding/memory/vibecop-combat-patterns.md
     -> Indexed into aq_agenttype_coding collection (document entry with embedding)

8. NODES: create_warning_task -> log_to_memory -> done
   Warning batched into medium-priority task. Info logged. Run completes.

9. PLAYBOOK RUN RECORDED
   PlaybookRun updated: status=completed, node_trace=[scan->triage->
   create_error_tasks->create_warning_task->log_to_memory->done],
   tokens_used=4200.

10. NEXT TIME: MEMORY IN ACTION
    Two days later, a coding agent works on mech-fighters/src/combat.py.
    The agent's Supervisor has memory tools available. While working on
    combat code, the LLM calls memory_search("combat system patterns").
    Multi-scope query fires:
      - aq_project_mechfighters (weight 1.0) -> project conventions
      - aq_agenttype_coding (weight 0.7) -> finds "vibecop frequently catches
        unhandled None checks in combat systems"
      - aq_system (weight 0.4) -> global conventions
    The insight is returned as tool output. The agent proactively handles
    None checks before committing, avoiding the vibecop finding entirely.
    The system got better.
```

## Supersedes

These specs have replaced (deprecated spec files removed):
- ~~`specs/rule-system.md`~~ — rules are now [[playbooks]] or [[vault|vault memory]]
- ~~`specs/hooks.md`~~ — hook engine replaced by [[playbooks|playbook executor]]
- Parts of `specs/agent-profiles.md` — profiles now stored as [[vault|vault markdown]]
