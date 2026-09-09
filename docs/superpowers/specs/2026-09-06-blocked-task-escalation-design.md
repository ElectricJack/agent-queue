# Blocked-task escalation playbook

**Date:** 2026-09-06
**Status:** shipped (default playbook, enabled)
**Source:** `src/prompts/default_playbooks/blocked-task-escalation.md`
**Reviewed bundle:** `tests/fixtures/playbooks/v2/blocked-task-escalation/`

## Problem

When a worker's session closes and the task lands in `BLOCKED`, nobody looks
at *why* unless a human happens to notice the Discord notice. The evidence is
in the tail of the session log, and the entity whose job is to read it and
decide is the project supervisor. Today nothing connects the two.

## What "task blocked" means here

Two unrelated facts share the word:

| Fact | Bus event | Meaning | Escalate? |
|---|---|---|---|
| A session close left the task `BLOCKED` | `task.failed` with `status: BLOCKED` | the agent could not finish: hard failure, retry budget spent, pipeline stopped short, attempt timed out | supervisor triage; human only if a real decision remains |
| The dependency graph's blocked projection flipped | `task.blocked` / `task.unblocked` | an upstream task is unfinished | no: there is no session to read |

The playbook triggers on the first and deliberately ignores the second. The
`task.failed` schema (`src/event_schemas.py`) carries `status`, `context`
(the close leg), `error`, `agent_id`, plus the base triple, so a subscription
filter of `status == "BLOCKED"` (the enum's upper-case wire spelling) selects exactly the terminal legs that
`_announce_close_outcome` in `src/orchestrator/execution.py` emits for.

## Design

One system-scoped rule, one command step, no LLM:

```
task.failed[status=BLOCKED]
  └─ message_send  to=session:supervisor-<project_id>  from=system/playbook:blocked-task-escalation
       ├─ queued        → completed
       └─ rejected / runtime_error → failed
```

The message names the task, quotes the close leg, the closing notes and the
agent, and instructs the supervisor to:

1. find the task's session in `aq session list` and read `aq session logs <session-id> -n 200`;
2. read `aq task show` and `aq task explain` for the task;
3. check whether an integration operation owns the repair/verification and preserve its controls
   and budgets;
4. decide: retry with concrete feedback (`aq task recover`), hold, or spawn a follow-up; create a
   source-bound durable escalation only when a real human decision remains.

Why a message rather than a task or an inline LLM step:

- **The supervisor already exists** as the project's named, on-demand
  session (`supervisor-<pid>`). The delivery cascade treats an absent
  supervisor address as `sleeping` and wakes it, so the notice reaches a live
  supervisor rather than spawning a one-off agent per blocked task.
- **Elevation.** A per-project supervisor token is elevated for its project,
  so it may run `session_logs`, `task_recover` and the rest. A task-scoped
  worker session cannot (`AGENT_COMMAND_SET` in `src/api/scope.py`), so an
  `ensure_task` with `profile_id: supervisor` would have produced an agent
  that could not read the log.
- **No contracted log reader.** An inline `llm` step may only call
  contracted commands and `session_logs` is not one, so it could not read the
  tail either.

## Mechanism added

`message_send` is now a contracted command (`src/commands/contracts/builtin.py`):
`MessageSendArgs` / `MessageSendValue`, business outcome `queued` (plus the
uniform `rejected`), side effect `create` on the new `EffectSubject.MESSAGE`,
idempotency `none`. `from_kind` defaults to `system`. The adapter re-enters
`CommandHandler.execute("message_send", …)` like every other built-in, so the
playbook's principal is what dispatch authorizes.

## Cardinality and failure

One triage message per blocked event: each terminal close is a distinct fact
and the supervisor's inbox coalesces delivery. Replayed human incidents reuse
the stable task-recovery source/incident key. A failed step (messages
substrate disabled, project missing) ends the run `failed` for the overlay; the
next blocked task starts a fresh run.

## Install

Like the other reviewed defaults: the source is copied to
`vault/system/playbooks/` by `ensure_default_playbooks`; the V2 artifact is
imported and activated per install
(`aq playbook v2-import --path tests/fixtures/playbooks/v2/blocked-task-escalation`,
then `aq playbook activate --playbook-id blocked-task-escalation --artifact-sha256 <hash>`).
