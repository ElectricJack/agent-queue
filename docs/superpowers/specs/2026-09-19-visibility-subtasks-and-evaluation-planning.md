# Planning: graph visibility, in-task subtasks, and a comparative evaluation

**Date:** 2026-09-19
**Status:** Planning — goals and workstreams agreed in outline, designs not yet written
**Source:** operator brainstorm, 2026-09-19

This is a planning document, not a design spec. It states where Agent Queue needs
to get to, where it is today, the workstreams that close the gap, and the open
questions each one has to answer before it earns its own design spec under
`docs/superpowers/specs/`.

---

## 1. The goal

**Agent Queue should be a better way to build software than a single agent
harness used alone.** "Better" is two numbers:

1. **Faster** — more work finished per unit of wall-clock time.
2. **Cheaper** — less usage spend for the same result, by routing each piece of
   work to the right intelligence class, using local agents where they suffice,
   and not paying frontier prices for mechanical work. This must hold even when
   the project itself is hard (advanced graphics, mathematics).

Two things follow from that goal and organise everything below:

- **The operator has to be able to see it working.** The dashboard's job is to
  answer "what is being worked on and how far along is it?" on landing, without
  digging. Today it does not (§2).
- **We have to be able to prove it.** A repeatable evaluation that runs the same
  project through Agent Queue, Claude Code alone and Codex alone, and tracks the
  result month over month (§5). This is also the evidence for anyone deciding
  between Agent Queue and a bare harness.

## 2. Where we are

| Area | Today | Gap |
|---|---|---|
| Graph shape | Server-side spatial layout (`src/task_graph/layout/`), hierarchy with containers, collapse/compaction derived per viewer (`compaction.py`). Built to stay fast at thousands of nodes. | The graph grows downward and never reorganises. Performance was the original concern; the real problem is that we show *everything* rather than the subgraph that matters now. |
| Hierarchy | `set_parent`, children/progress reads in `hierarchy_queries.py` (`total/done/ready/blocked/in_progress`). | No notion of a *phase*. Ordering between bodies of work needs explicit per-task dependencies. |
| Dynamic tickets | Tasks can be filed with `--parent`; nothing requires it. | Derivative work lands in the project root and makes it busy. No recorded "spawned by" relationship to draw. |
| Old work | `Orchestrator._auto_archive_tasks` and `archived_tasks` exist; archive keeps rows searchable. | Finished work still dominates the view. Archival is not tuned for "keep the canvas about now", and is per-task rather than per-finished-subtree. |
| Progress | Aggregates exist on layout nodes (`agg_running`, `agg_blocked`). | Progress bars are not consistently shown on epics, children and (future) phases. |
| Agent markers | Layout nodes carry `assigned_agent_id`; the canvas draws a marker from it. | Markers dangle: a node shows an agent after the task is done or when no session is live. |
| Subtasks | A task is the smallest tracked unit, and a task implies an agent, a branch and a merge. | No lightweight checklist a single agent can tick off inside one task. |
| Measuring success | Fleet metrics, `task_recent_activity` with per-attempt model attribution, provider-usage work. | No comparative evaluation against bare harnesses; no trend line. |
| Repo cleanliness | Branch/merge policy works (integration trains, branch discard). | "Works, but not great", and unmeasured. |

## 3. Workstream A — a graph you can read on landing

**Outcome:** open the graph tab and immediately see what is in flight, grouped
sensibly, with progress visible at every level.

### A1. Show subgraphs, not the database

The default view is the *active* subgraph: epics with open work, expanded to the
level where something is happening. Everything else is reachable but not drawn.
The persisted layout stays the fully expanded geometry; this is a question of
the default expanded set and of what is archived (A4), which the compaction
layer already supports.

Intended hierarchy: **epic → child task → subtasks** (subtasks per workstream C —
progress trackers, usually not separately scheduled).

### A2. Phases

A phase is a labelled, ordered grouping — at project level (outside epics) and
possibly inside an epic.

- Phases are ordered: phase 1, then 2, then 3. Each may carry a label.
- **Phases gate implicitly.** Every ticket in phase *N+1* is blocked until phase
  *N* completes — no per-ticket dependency edges.
- Phases get a progress bar.

Open questions:
- Is a phase a new container kind in the existing hierarchy (cheapest: reuse
  `set_parent`, progress reads, layout containers) or a separate attribute on
  tasks? Recommendation to test first: a container kind with an `order` and a
  gate rule between sibling phases.
- Where does the gate live? Admission (the claim frontier / promotion cascade)
  must see it, so it needs to be a cheap, index-friendly predicate — the claim
  query was just made an ordered index scan and must stay one.
- "Complete" for gating: all children terminal, or all children *delivered*
  (merged)? Trains projects probably want delivered.
- What happens when a ticket is added to an already-completed phase?
- Formulas (`aq-graph` blocks) should be able to declare phases.

### A3. Dynamic tickets never land in the root

When a running task spawns work, the new ticket defaults to:

1. a **child** of the spawning task, when it is a decomposition of it; or
2. a **sibling inside the spawning task's epic**, when it is follow-up or
   discovered work.

Root placement becomes an explicit choice, not the default. The spawn
relationship is recorded and drawn. The creation path can infer the spawner from
the session's claim (`.aq/claim.json` / `AQ_SESSION_ID`), so the agent does not
need to pass it.

Policy note: *which* of child/sibling applies is policy and belongs in a
playbook or a creation-time rule, with the mechanism (infer spawner, record
provenance, refuse silent root placement) in code.

Open questions: what about supervisor- and playbook-created tasks (CI sentinel
repairs, recovery incidents) — a standing "maintenance" epic per project? How
does a child added to a closing parent behave under the existing close-subtree
semantics?

### A4. Get old work out of the way

Finished work is archived automatically — still in the database, still
referenceable, still searchable — so the canvas is about now.

Open questions: archive unit (whole finished epic/phase subtree vs. individual
tasks — subtree is tidier and avoids half-archived containers); grace period
before archiving so a just-finished epic is visible for a while; a "show
archived" toggle and search that reaches archived rows from the graph; how
archived subtrees interact with layout (`layout_dirty`, reclaiming space).

### A5. Progress bars everywhere

Epics, children with subtasks, and phases all show done/total. The aggregate
reads exist; this is mostly making them uniform across container kinds and
including subtasks (C) in the child's bar.

## 4. Workstream B — accurate agent markers

**Outcome:** a marker on a node means a live agent is working that task right
now. No marker means no one is.

Symptom: markers remain on finished tasks, or on tasks where opening the ticket
shows nothing active.

Leading hypothesis to confirm (not yet verified): the marker is drawn from
`assigned_agent_id` on the layout node, which is *assignment*, not *liveness*.
Assignment can outlive the attempt (completion, stall, session death, paused
task, retained owner), and a layout node only refreshes when something marks it
dirty. The digest already has the right definition — "active means a live,
non-stale running attempt, never `updated_at` churn, a heartbeat, a stalled
session or a worker parked on a human answer" (`digest_queries.py`). The marker
should use the same definition.

Plan:
1. Reproduce and catalogue: for each dangling marker found, record task status,
   `assigned_agent_id`, attempt rows and session state. Decide whether the bug is
   stale data (assignment not cleared), stale projection (layout not re-marked
   dirty on the transition) or stale client (WebSocket update missed / store not
   reconciled).
2. Fix at the source, then make the marker derive from live-attempt truth.
3. Add a doctor check that flags assignment-without-live-attempt so regressions
   surface without someone staring at the graph.

This is a bug, so it goes through systematic debugging rather than design.

## 5. Workstream C — subtasks inside a task

**Outcome:** a child task can carry many small subtasks. One agent (Claude Code
or Codex, possibly with its own sub-agents) works the task and ticks subtasks
off as it goes. The graph shows that progress. No agent, branch or merge per
subtask.

Why not an agent per subtask: sequential execution by one agent that already
holds the context is often simply faster. We want the visibility, not the
scheduling.

What is needed:
- **A subtask record** that is durable and visible but *not schedulable*: never
  on the claim frontier, never owns a branch, never merged on its own. It rolls
  up into its parent's progress.
- **A report-back call** from the working agent, over CLI and MCP (one
  `CommandHandler` command, both surfaces): mark subtask done / in progress,
  optionally with a note.
- **A context read**: list my task's subtasks; fetch the full context for one.
  Agent Queue is the durable store of what needs doing and why — the agent
  should be able to pull detail per subtask rather than carry it all in its
  prompt.
- **Prompt/skill changes** so worker harnesses know the subtask list exists and
  are told to report as they go (prime output, `aq-tasks` skill).
- **Merge policy review.** Today a task maps to a branch and a delivery. Subtasks
  must be explicitly outside that: they complete inside the parent's branch and
  are delivered with it.

Open questions:
- Is a subtask a row in `tasks` with a non-schedulable flag (reuses hierarchy,
  progress, layout, archive for free; risks leaking into every query that
  assumes task ⇒ schedulable) or a separate lightweight table (clean separation;
  must be taught to layout and progress)? This is the main design decision.
- Can a subtask be *promoted* to a real task when it turns out to be big or
  parallelisable? That is the natural bridge to routing/cost decisions.
- What does closing the parent do with unticked subtasks — refuse, warn or
  auto-skip?
- Who authors subtasks: the planner at decomposition time, the worker itself on
  first read, or both?
- If the agent never reports, the bar lies. Do we nudge (message cascade), or
  infer from commits? Prefer a nudge; never infer completion.

## 6. Workstream D — a comparative evaluation

**Outcome:** one command runs a fixed project three ways — Agent Queue, Claude
Code alone, Codex alone — and produces a scored report comparable with last
month's.

### D1. The benchmark project

Tension to resolve: it must be **large** (Agent Queue's advantage is large
projects) and **hard** (it has to show we still solve hard problems), yet
**affordable** enough to run monthly without consuming our usage.

Requirements:
- A written spec all three arms receive verbatim. The bare-harness arms get a
  standard, fair prompt — not a strawman.
- An objective acceptance suite the arms never see (hidden tests) so
  correctness is measured, not judged.
- Natural decomposability *and* at least one genuinely hard core, so routing has
  both cheap and expensive work to distinguish.
- Pinned starting repo, pinned model versions recorded per run.
- Possibly two sizes: a small "smoke" project for cheap, more frequent runs and
  the full one monthly.

### D2. Metrics

| Metric | How |
|---|---|
| Wall-clock time to completion | Start of run → acceptance suite passes (or budget exhausted). |
| Usage spend | Tokens and cost, per model. For Agent Queue from `task_session_attempts` / provider-usage; for bare harnesses from their own usage output. Same accounting for all arms. |
| Autonomy | Count of human-in-the-loop requests (escalations, gates, questions). Zero is the target. A scripted "operator" answers consistently across arms, and each intervention is counted. |
| Correctness | Hidden acceptance suite pass rate. |
| Output quality | Blind code review of each result against a fixed rubric (LLM judge with the arm identity hidden; periodic human spot-check to calibrate the judge). |
| Repo cleanliness | See E. |
| Routing efficiency (Agent Queue only) | Spend share by intelligence class; rework/retry rate; share of tasks completed at the first-assigned class. |

Headline numbers are the two from §1 — time and spend — *conditional on passing
correctness*. A fast, cheap wrong answer scores zero.

### D3. Harness

- One entry point (`aq eval run <benchmark>`), disposable environment (the e2e
  kit and the throwaway-container install test are the precedent), no contact
  with the operator's database.
- Results stored durably with the Agent Queue commit SHA, config, model IDs and
  benchmark version, so month-to-month comparison is honest and a benchmark
  change starts a new series.
- A report/dashboard view showing the trend per metric and per arm.
- Hard budget ceiling per arm; a run that hits it is recorded as such, not
  discarded.

Open questions: variance — a single run per arm is noisy; how many repetitions
can we afford, and do we report ranges? How do we keep the benchmark from
leaking into model training or into Agent Queue's own memory between runs (fresh
vault/memory per run)? Do we also evaluate Agent Queue with learned memory
*retained*, since "gets better with use" is the core claim — i.e. a fourth arm?

### D4. Cadence

Monthly, plus on demand after a change expected to move the numbers. Not
continuous — it is expensive by nature.

## 7. Workstream E — repo cleanliness

Managing the project repository is part of the product, so it is part of the
score. Candidate measurable signals, taken at end of run:

- Stale/unmerged branches left behind; orphaned worktrees.
- Commit history: merge-commit noise, fix-up churn, revert count, commit message
  quality.
- Stray files (scratch output, logs, agent notes) committed to the tree.
- Conflicts encountered and how they were resolved; rework caused by merges.
- Default branch always green (CI state at each merge).

Two uses: a metric in the evaluation (D2), and a standing input to improving
branch/merge policy. Open question: the bare-harness arms work on one branch, so
some signals are trivially clean for them — score only what is comparable, and
track the rest as an Agent Queue-internal trend.

## 8. Sequencing

1. **B — dangling markers.** A bug; small; restores trust in the view. Start now.
2. **A3 + A4 — placement and archival.** Cheapest big win for readability; mostly
   policy and defaults over existing mechanisms.
3. **D (benchmark choice + harness skeleton) in parallel.** The first run is the
   baseline; everything after it is measured against that, so the earlier it
   exists the more the rest of this plan can be verified rather than asserted.
4. **C — subtasks.** Needs the schema decision and the merge-policy review.
5. **A2 — phases.** Touches admission; design carefully against the claim
   frontier.
6. **A1 + A5 — default subgraph view and uniform progress bars**, once phases and
   subtasks exist to be shown.
7. **E** folds into D as its metrics are defined.

Each numbered workstream gets its own design spec before implementation.

## 9. How we know this plan worked

- Landing on the graph answers "what is in flight and how far along" without
  scrolling or expanding; the root holds epics/phases, not loose tickets.
- Zero markers without a live attempt (enforced by the doctor check).
- A multi-subtask task shows its bar moving while a single agent works it.
- The evaluation has a baseline and at least two subsequent monthly runs, and
  the time and spend trend lines for Agent Queue are moving the right way
  relative to both bare harnesses at equal or better correctness.
