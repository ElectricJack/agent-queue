---
tags: [design, overview, index]
---

# Next-Generation Design Specs

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

These specs describe the automation and knowledge systems of Agent Queue.
They are **design documents** that serve as the architectural reference for the current implementation.

## Documents

### Framework overhaul (2026-08-19) — approved direction, drafts

Direction: [../analysis/framework-overhaul-todo.md](../../analysis/framework-overhaul-todo.md) (v3).
Each has a paired implementation spec in [`docs/specs/implementation/`](../implementation/README.md).

| Spec | Workstream | Summary |
|---|---|---|
| [session-runtime](session-runtime.md) | A | tmux-first session providers, harness profiles, task/named lifecycles, close→drain-ack completion, adoption, stall ladder, transcript readers |
| [worktree-execution](worktree-execution.md) | W | Per-agent-slot worktrees under `<repo>/.aq/worktrees/`, branch-per-task, merge slot, slot reaper (amends [workspaces-v2](workspaces-v2.md)) |
| [work-graph](work-graph.md) | D | Typed dependency edges, persisted `is_blocked`, gates as records, labels, outcome metadata, `task explain`, state-machine enforcement |
| [supervisor-agent](supervisor-agent.md) | B | Supervisor as a configured per-project agent; `messages` table; chat relay; spec→task-graph |
| [aq-surface](aq-surface.md) | C | `aq` CLI as primary surface, `aq prime`/handoff/inbox, JSON envelope, session tokens, task-scoped MCP allowlist |
| [messaging-rework](messaging-rework.md) | F | Historical messaging rework, superseded for Discord by [../messaging/discord](../messaging/discord.md) |
| [feature-pauses](feature-pauses.md) | E/P | Memory & playbooks paused (flags off, code frozen, data preserved, clean re-enable) |
| [trust-and-ops](trust-and-ops.md) | G | Trust boundaries, env scrubbing, `aq doctor`, `aq costs`, invariant tests |

> While the overhaul lands, [playbooks](playbooks.md), [memory-plugin](memory-plugin.md), [memory-scoping](memory-scoping.md),
> [self-improvement](self-improvement.md) and [agent-coordination](agent-coordination.md) describe **paused** subsystems — kept for
> the comeback (see [feature-pauses](feature-pauses.md)); [workspaces-v2](workspaces-v2.md) is amended by [worktree-execution](worktree-execution.md).

### Pre-overhaul specs

| Spec | Status | Summary |
|---|---|---|
| [guiding-design-principles](guiding-design-principles.md) | Active | The 10 core principles behind all design decisions |
| [playbooks](playbooks.md) | Paused | Agent workflow graphs — directed graphs of LLM decision points, replaced rules + hooks |
| [vault](vault.md) | Active | Vault directory structure, what lives where, reference stubs, Obsidian integration |
| [memory-plugin](memory-plugin.md) | Active | Memory plugin v2 architecture, memsearch fork, Milvus backend with KV storage |
| [memory-scoping](memory-scoping.md) | Active | Scope hierarchy, overrides, multi-scope query, agent MCP tools, deduplication |
| [profiles](profiles.md) | Active | Agent profiles as markdown, hybrid format, sync model, starter knowledge packs |
| [self-improvement](self-improvement.md) | Active | Self-improvement loop, orchestrator memory, reflection, health & observability |
| [agent-coordination](agent-coordination.md) | Active | Playbook-driven multi-agent coordination, workflows, agent affinity, workspace strategies |
| [roadmap](roadmap.md) | Active | 196-task implementation roadmap across 8 phases with dependencies and test checkpoints |

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

All specs share the same [design principles](guiding-design-principles.md).
The [vault](vault.md) is the prerequisite for [playbook](playbooks.md) storage.
[Playbooks](playbooks.md) are the mechanism for the
[self-improvement loop](self-improvement.md).
[Coordination playbooks](agent-coordination.md) extend the playbook model to
multi-agent workflows. [memory-plugin](memory-plugin.md) and [memory-scoping](memory-scoping.md) define how
all memory operations work.

### Prerequisite Refactors (in [playbooks](playbooks.md) Section 17)

Several existing subsystems need changes before playbooks can be implemented:
- **EventBus payload filtering** — needed for cross-playbook composition
- **Event schema registry** — enforce event payload contracts
- **GitManager event emission** — `git.commit`, `git.push`, `git.pr.created`
- **Supervisor config flexibility** — per-call model/tool overrides
- **Unified vault file watcher** — one watcher for the whole vault
- **Task records migration** — move out of memory/ to stop polluting search

## End-to-End Trace

A concrete walkthrough showing [playbooks](playbooks.md), [memory-scoping](memory-scoping.md), and
[agent-coordination](agent-coordination.md) working together:

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
- ~~`specs/rule-system.md`~~ — rules are now [playbooks](playbooks.md) or [vault memory](vault.md)
- ~~`specs/hooks.md`~~ — hook engine replaced by [playbook executor](playbooks.md)
- Parts of `specs/agent-profiles.md` — profiles now stored as [vault markdown](vault.md)
