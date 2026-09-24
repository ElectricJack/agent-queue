# Real-time agent collaboration — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [agent sleep/wake](2026-09-24-agent-sleep-wake.md) ·
[exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[wake context compaction](2026-09-24-wake-context-compaction.md) ·
[mini-projects](2026-09-24-mini-projects.md) ·
[adversarial review recipe](2026-09-24-adversarial-review-recipe.md) ·
existing: [agent coordination](../../specs/design/agent-coordination.md) ·
[messaging rework](../../specs/design/messaging-rework.md) ·
[live pane streaming](2026-08-25-live-pane-streaming-design.md) ·
[worktree execution](../../specs/design/worktree-execution.md)

## 1. The ask

Two (or more) agents pair-programming or working on a shared goal, each able to
read the other's output **live**. The operator put it this way: "Messaging
exists; sustained collaboration doesn't… interesting, but a big design problem,
and the job/event infrastructure (job queue + agent sleep/wake) will shape how
you'd build it." Priority: **backlog**. The purpose of this document is to lay
out the design space so the sleep/wake and job-queue work does not close off
the options.

## 2. What exists today

**Durable agent-to-agent messages.** `src/commands/message_commands.py`
provides `message_send` (`:149`), `message_reply` (`:284`), `message_inbox`
(`:371`) and `message_status` (`:513`). The `messages` table
(`src/database/tables.py:1290`) has `thread_id` (indexed, `:1322`),
`reply_to_id`, `priority` and `body_kind`. The recipient kinds are
`{session, task, profile, user}`. All three commands are in the non-elevated
`AGENT_COMMAND_SET` (`src/api/scope.py:38-40`) and in both worker templates'
grants (`src/profiles/defaults/worker-claude/profile.md:74-76`), so **any
worker can message another worker's task today**. The `aq-comms` skill
(`src/skills/aq-comms/SKILL.md`) documents agent→agent messages as
"coordination".

**Delivery.** `MessageDeliveryEngine.run_delivery_pass`
(`src/messages/delivery.py:74`) runs as a cascade step every cycle, about
every 5 s. It asks `SessionLens.activity` (`src/messages/session_lens.py:194`)
for one of `idle|busy|sleeping|absent`. A session counts as *busy* if it
produced output in the last 30 s (`_BUSY_WINDOW_SECONDS`, `:65`), and busy
sessions are skipped (`delivery.py:119`). An *idle* session receives **one**
message per pass, typed in as a one-line nudge,
`Handle \`aq message status <id> --json\`.` (`_render_nudge`, `:388`). A
*sleeping* recipient is cold-started only if it is the supervisor
(`session_lens.py:235`: "Only supervisor-named sessions are wake-on-demand").
For task recipients, *absent* means the message waits for the next `aq prime`
(`src/prime/sections.py:454`, `build_messages_section`). There is **no
mid-turn injection**: the `UserPromptSubmit` inject hook was removed on
2026-08-27 (per CLAUDE.md and `aq-comms`). A transcript-tail fallback
(`check_reply_timeouts`, `:168`) turns an unreplied message's next assistant
turn into a reply.

**Waiting is penalised.** A task or pool session with no pane activity for
`sessions.lease_ttl_seconds` (default 480, `src/config.py:1312`) climbs the
stall ladder of nudge, backoff, restart and quarantine
(`SessionReconciler._step_stall_ladder`, `src/sessions/reconciler.py:1300`).
The one carve-out is a session waiting on an agent question
(`_waiting_for_question`, `:1229`). The supervisor profile explicitly bans
"background inbox polling loop[s] or shell sleep loop[s]"
(`supervisor/profile.md:175-176`).

**A long-poll exists, but only for claims.** `aq task close --claim-next --wait N`
(`src/cli/agent_surface.py:309`) long-polls on the server and calls
`touch_session_activity` while it waits (`src/commands/claim_commands.py:276`),
so the stall ladder does not fire. This is the pattern a "wait for partner"
primitive would copy.

**Sleep/wake for sessions** is limited to *named* sessions:
`session_sleep`/`session_wake` (`src/commands/session_commands.py:559/569`)
refuse task sessions ("a task session is started by the task lifecycle").
The general primitive belongs to the [sleep/wake spec](2026-09-24-agent-sleep-wake.md).

**Reading another agent's output.** Humans have `session_peek` (`:218`) and
the live pane stream (`PaneBroadcaster`, `src/sessions/pane_broadcaster.py`,
`GET /api/sessions/{id}/pane`, from the 2026-08-25 design). None of these is in
`AGENT_COMMAND_SET`, so **a worker cannot tail another worker**. What a worker
*can* read is `task_show`, `task_comments`, the subtasks of any task in its
project, and whatever a partner pushes to a git branch (`git_push`/`git_diff`
plugin tools in the worker grants).

**Workspaces block shared writing.**

- `project-repo` defaults to an exclusive lock (`workspaces-v2.md:153`).
  `WorkspaceMode.BRANCH_ISOLATED` is now a deprecated alias for EXCLUSIVE, and
  `DIRECTORY_ISOLATED` raises `not yet implemented`
  (`src/models.py:188-200`, `src/orchestrator/workspace.py:210`).
- Worktree slots give parallelism across *branches*. However, git allows a
  branch to be checked out in only one worktree, so two tasks on one branch
  serialize with a 60 s PAUSED backoff (`worktree-execution.md:221-223`: "A
  parallel plan therefore executes serially").

**Coordination structure.** Structure exists (playbook `agent_task` steps,
`wait` steps of kind `task`/`event`, task affinity via
`affinity_agent_id` in `src/scheduler.py:504`, graph edges, phases), but all
of it is **turn-based at task granularity**: agent B starts when agent A's
task completes.

## 3. Gaps

1. **No cheap "wait for partner".** An agent that waits either burns turns
   polling, which is banned and wastes tokens, or sits idle and gets
   stall-nudged after 8 minutes.
2. **No live read of the partner.** The only live channels are git pushes and
   task comments, and both require the writer to publish deliberately.
3. **Delivery latency is one turn.** A busy partner receives a message only
   when it next goes idle, one message per 5-second pass.
4. **No shared scratch space** with ordering: a document both can append to
   and read the latest of, such as a pair's "whiteboard".
5. **Workspace model forbids two writers** on one branch or one worktree.
6. **No pairing entity.** Nothing records "tasks X and Y are a pair", so a
   pair cannot be addressed as a unit and the system cannot keep them
   co-scheduled (both running at once) or tear them down together.

### 3.1 Collaboration modes, and which gaps block each

| Mode | Shape | Blocked today by |
|---|---|---|
| M1 Driver/navigator, one workspace | driver writes; navigator reads the same tree, comments | exclusive lock; no second session in one worktree; navigator can't read driver's pane |
| M2 Two writers, one branch | both edit and commit to `aq/<x>` | git single-checkout rule (serializes); merge races |
| M3 Two writers, two branches, shared goal | each owns a branch; exchange diffs/messages | wait cost (gap 1), latency (gap 3), later fold of branches (integration) |
| M4 Reviewer tailing a writer | reviewer follows writer's WIP and posts findings as they go | no agent-readable live feed (gap 2); wait cost |
| M5 Shared scratch/channel | agents converse on a thread toward a shared artifact (design, debug) | wait cost; no ordered shared doc; no pair entity |

M3, M4 and M5 fit the existing isolation model. M1 and M2 fight it.

## 4. Implementation options

### Option A — "Pair channel" on existing messages + a wait primitive

A pair is two ordinary tasks linked by a `related` edge (non-blocking, from
`work-graph.md` §3.2) plus a shared `thread_id` (for example `pair:<uuid>`).
The additions are:

1. `aq message wait --thread <id> [--from task:<partner>] --timeout <s>`: a
   server-side long-poll modelled on the claim wait, which returns the next
   pending message and touches session activity. This is the cheap wait,
   bounded by the harness's tool timeout, which is about 10 minutes for
   Claude Code's Bash tool (unverified for codex).
2. A prime and skill convention: "when paired, post progress to the thread;
   block on `message wait` when you need your partner".
3. Optionally, a `pair_create` command that files both tasks, the edge and the
   thread, and pins co-scheduling with the same priority and affinity hints.

- **Touches:** `message_commands.py`, `message_queries`, the CLI,
  `aq-comms`, and possibly stall-ladder awareness.
- **Pros:** small. It builds on audited delivery. It works for M3, M4 and M5.
  It uses no tokens while waiting.
- **Cons:** the wait is bounded by the tool timeout, so a long wait needs a
  re-issue loop. There is still no live read of the partner's work, only what
  it posts.
- **Size:** S–M.

### Option B — Sleep-until-event (depends on sleep/wake)

The agent ends its turn with `aq session sleep --until message:thread=<id>`
(or `--until task:<id>.completed`). The daemon parks the session, suspending
or stopping it, preserving the harness transcript and releasing or keeping the
workspace per policy. The delivery engine's existing `sleeping` branch
(`delivery.py:123`) generalises from "supervisor only" to "any session that
declared a wake condition", which removes the `session_lens.py:235`
restriction under a durable ownership rule. On wake, the prime or a nudge
renders the partner's message, compacted if the
[wake-context-compaction](2026-09-24-wake-context-compaction.md) spec applies.

- **Touches:** the session lifecycle, the reconciler (a sleeping task session
  is neither stalled nor dead), the claim and lease semantics (does a sleeping
  holder keep its claim?), and the delivery engine.
- **Pros:** unbounded waits with no idle cost. It is the same primitive the
  job queue needs ("wake me when my test job finishes").
- **Cons:** this is the hard part of the sleep/wake spec, and collaboration
  cannot be simpler than it.
- **Size:** L. Most of it is owned by the sleep/wake spec, with S on top for
  collaboration.

### Option C — Agent-readable live feed of a partner

Give paired tasks a scoped read of each other:

- a `pair_peek` (`session_peek` fenced to the partner's session, text only);
- or `task_diff` (the partner's worktree `git diff`, read-only);
- or a WIP-push convention, where the writer runs `git_push` to its branch
  every N minutes and the reviewer runs `git fetch`/`git_diff`.

This enables M4 and the "navigator" half of M1 without sharing a worktree.

- **Touches:** `src/api/scope.py` (a new fenced command), session commands,
  and the pairing record, which the fence needs.
- **Pros:** real "reading each other's output". It reuses `peek`.
- **Cons:** pane text is noisy TUI output (the reason the live-pane design
  rejected raw bytes). It is a new cross-task read surface, and so a security
  review item. The partner's context gets polluted.
- **Size:** M.

### Option D — Shared-workspace pair (M1/M2 proper)

A second session attaches to the first task's worktree:

- read-only for the navigator; or
- write-enabled with a turn token (driver ↔ navigator swap) held in the DB.

This needs a workspace "co-occupant" concept, since `DIRECTORY_ISOLATED` is a
stub, an edit-conflict policy, and changes to the sentinel and lock
(`workspace.py` layer-2 sentinel).

- **Pros:** the truest pair programming.
- **Cons:** it cuts against the one-task-one-worktree invariant that
  delivery, integration, cleanup and recovery all assume.
- **Size:** XL.

### Beyond the options — a first-class "collaboration session" entity

This is a `collab` record with members, roles, a shared ordered scratch
document (a vault file or a review-style DB doc), a turn protocol, a budget,
and a lifecycle, driven by a playbook: `agent_task` steps with
`wait_for_completion: false` plus `wait` steps of kind `event` on
`message.sent`/thread. It is the long-term home for A, B and C.

- **Size:** XL.

## 5. Initial take

*Provisional.* Collaboration should be a **consumer** of the sleep/wake and
wake-on-event work, not a driver of it. The sequence would be:

1. Now, nothing ships. Make sure the [sleep/wake](2026-09-24-agent-sleep-wake.md)
   design treats "message on thread T" and "task X reached state S" as
   first-class wake conditions, alongside "job finished".
2. As a first slice, ship **Option A's `message wait`** long-poll. It is cheap,
   useful beyond pairing (a worker waiting for a supervisor answer), and gives
   real data on whether agents collaborate usefully at all.
3. Then **B** once sleep/wake lands, and **C's WIP-push convention**, which is
   docs only, for M4.
4. Defer D, and only consider the first-class entity after A, B and C show real use.

M3, M4 and M5 cover most of the value with no change to workspace
invariants. M1 and M2 are expensive and it is unclear they beat
"navigator reviews pushed WIP every few minutes".

## 6. Open questions

1. **Is the target M4/M5 ("agents talk while working") or M1/M2 ("agents
   type into the same tree")?** This decides whether the workspace model
   changes at all (D) or not (A/B/C).
2. **Does a sleeping task session keep its claim and workspace?** Keeping them
   blocks the slot. Releasing them risks losing the worktree state it needs on
   wake. This is shared with the sleep/wake and job-queue specs, and
   collaboration should adopt their answer rather than invent one.
3. **Should a paired partner be able to *interrupt* a busy agent,** or is
   next-idle delivery enough? Interruption means mid-turn injection, which was
   removed on 2026-08-27 on purpose.
4. **What is the pairing record:** a `related` edge plus a `thread_id`
   convention, task metadata, or a new table? This decides whether fences
   ("may task X peek task Y?") can be enforced in SQL.
5. **Co-scheduling:** must both halves run at once, gang-scheduled, or is
   asynchronous turn-taking acceptable? Gang scheduling interacts with pool
   sizing (`src/orchestrator/pools.py`) and provider availability. If one half
   is held on a provider outage, the other waits forever.
6. **Cost guard:** what bounds a pair that ping-pongs forever? Candidates are
   a per-thread message cap, a token budget, or a wall-clock limit. The digest
   and escalation paths would need to surface a runaway pair.
7. **Cross-family pairs** (Fable + Astra, per the adversarial recipe): same
   design, or does one harness (codex) lack something the wait/wake path
   needs, such as a harness-specific tool timeout or resume support?
8. **What does the operator see?** A pair view on the dashboard (two pane
   tiles plus the thread), or just the thread in the task panel?

## 7. Dependencies and sequencing

- **Hard dependency:** [agent sleep/wake](2026-09-24-agent-sleep-wake.md) for
  Option B. It also shares the claim/lease question with the
  [exclusive job queue](2026-09-24-exclusive-job-queue.md).
- **Soft dependency:** [wake context compaction](2026-09-24-wake-context-compaction.md),
  since a woken partner needs a tight summary of what it missed.
- **Enables:** a live form of [mini-projects](2026-09-24-mini-projects.md)
  (agents debate on a thread instead of in rounds), and a live form of the
  [adversarial review recipe](2026-09-24-adversarial-review-recipe.md).
- Option A's `message wait` can land independently at any time.

## 8. Non-goals

- Humans pairing with agents in the terminal. `tmux attach` and the live pane
  stream already cover this.
- Multi-agent editing of one worktree in the first slices (Option D).
- A general chat product. The thread is a coordination channel, not a UI.
- Replacing task-granular coordination (graphs, phases, playbooks). Pairs sit
  inside that structure.
