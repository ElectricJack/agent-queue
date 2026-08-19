---
tags: [implementation, overview, index, overhaul]
---

# Framework Overhaul — Implementation Specs

Paired implementation plans for the 2026-08 framework overhaul. Each file here has a
matching **design spec** in `docs/specs/design/` under the same name; the design spec says
*what and why*, the implementation spec says *where and how* — exact modules, schema
migrations, signatures, integration points, phase checklists, test plans, and rollout flags.

Direction source: [../analysis/framework-overhaul-todo.md](../../analysis/framework-overhaul-todo.md)
(v3, all open questions resolved 2026-08-19). Background:
[../analysis/comparison-gascity-beads.md](../../analysis/comparison-gascity-beads.md).

| Spec | Workstream | Pairs with |
|---|---|---|
| [session-runtime.md](session-runtime.md) | A — tmux-first session providers, harnesses, lifecycles, adoption, transcripts | [design](../design/session-runtime.md) |
| [worktree-execution.md](worktree-execution.md) | W — per-agent-slot worktrees, merge slot, reaper | [design](../design/worktree-execution.md) |
| [work-graph.md](work-graph.md) | D — typed edges, `is_blocked`, gates, labels, outcomes, explain | [design](../design/work-graph.md) |
| [supervisor-agent.md](supervisor-agent.md) | B — supervisor as configured agent, messages, spec→graph | [design](../design/supervisor-agent.md) |
| [aq-surface.md](aq-surface.md) | C/G.2 — `aq` CLI, prime/handoff, JSON envelope, tokens, MCP allowlist | [design](../design/aq-surface.md) |
| [messaging-rework.md](messaging-rework.md) | F — out-of-process Discord adapter, task threads, dashboard | [design](../design/messaging-rework.md) |
| [feature-pauses.md](feature-pauses.md) | E/P — memory & playbooks paused, not removed | [design](../design/feature-pauses.md) |
| [trust-and-ops.md](trust-and-ops.md) | G — trust boundaries, env scrubbing, doctor, costs, invariants | [design](../design/trust-and-ops.md) |

## Build order (from the overhaul doc §11)

```
Phase 0  feature-pauses + work-graph (+ aq-surface MCP allowlist, trust-and-ops scrub/doctor skeleton)
Phase 1  session-runtime + worktree-execution (built together: a session's work_dir is its slot worktree)
Phase 2  supervisor-agent + aq-surface (full CLI) + messaging-rework F.1 (thread reply→nudge)
Phase 3  messaging-rework F.2 (dashboard), trust-and-ops doctor completion
Phase 4  comebacks: memory re-enabled through prime; playbooks redesigned (see feature-pauses §un-pause)
```

## Cross-cutting contracts

- **Session naming**: consumers use *logical* names (`supervisor-<pid>`); only the session
  manager constructs provider names (`s-<task_id>`, `n-<profile>[--<project>]`).
- **Event names & payloads**: owned by session-runtime; every emitted type must have a
  registered payload schema (test-enforced, trust-and-ops).
- **Completion protocol**: `aq task close … && aq session drain-ack`; process exit = failure
  signal. Outcome/work-state metadata keys are defined in work-graph.
- **Untrusted data never reaches `sh -c`** (trust-and-ops R-rules); session env is scrubbed.
