# Mini-projects (ideation sessions) — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [adversarial review recipe](2026-09-24-adversarial-review-recipe.md) ·
[real-time agent collaboration](2026-09-24-realtime-agent-collaboration.md) ·
[morning report playbook](2026-09-24-morning-report-playbook.md) ·
existing: [formulas](../../specs/design/formulas.md) ·
[work graph §13b phases](../../specs/design/work-graph.md) ·
[planning emits phases and subtasks](2026-09-20-planning-emits-phases-and-subtasks-design.md) ·
[agent coordination](../../specs/design/agent-coordination.md) ·
[document reviews guide](../../guides/reviews.md)

## 1. The ask

A lightweight session type for ideation. A few agents take in a brain dump
(like the operator's note that produced this spec set), argue it out, with one
of them running the brainstorming skill and the others critiquing, and
converge on a roadmap. Priority: **backlog**.

**This document set is itself an instance of the workflow.** One operator
brain dump went to a coordinating agent. That agent fanned out to several
sub-agents, each writing two or three grounded preliminary specs in parallel,
and a separate pass writes the index roadmap
([2026-09-24-operator-feedback-roadmap.md](2026-09-24-operator-feedback-roadmap.md)).
It ran on **harness-level sub-agents** (Claude Code's `Task` tool), not on AQ
tasks. It therefore got one model family, no durable per-agent record, no
critique round, and no human gate before the roadmap. Those missing pieces are
roughly what a first-class mini-project would add.

## 2. What exists today

**Graphs with phases.** An `aq-graph` document can declare
`phases: [{key, title, label?}]` and set `phase:` on each node
(`GraphPhase`, `src/task_graph/models.py:122`; work-graph §13b).
`create_task_graph`/`formula_cook` write the epic, the phase containers, the
inter-phase `blocks` gates and the tasks in one transaction
(`src/task_graph/creator.py`). Phase *N+1* stays DEFINED until phase *N*
settles. Nodes carry `profile`, `intelligence_class`, `pin`, `acceptance`,
`deliverables`, `context` and `subtasks` (`GraphNode`, `models.py:141`).
Limits:

- Phases need a root placement (`graph.phases_need_root`,
  `src/commands/task_commands.py:3452`).
- Phases are refused in `hierarchy`/`train` projects
  (`hierarchy.phases_unsupported_mode`).
- The graph is static: it has no loops, and the only conditional is
  `conditional-blocks`, which runs a node only if a dependency failed
  (`work-graph.md:67`).

**Formulas.** A formula is a vault-authored graph with `vars` and `extends`
(`src/task_graph/formulas.py`, patterns `formulas/*.md` and
`projects/*/formulas/*.md`, `FORMULA_PATTERNS` at `:58`). It is cooked with
`aq formula cook`.

- `formula_cook` is **refused to non-elevated sessions**
  (`src/commands/formula_commands.py:266-269`), so only the operator's CLI or
  the supervisor can start one.
- **No formulas ship.** There is no default-formula seeding: the only formula
  files are test fixtures in `tests/fixtures/formulas/`.
- `create_task_graph` *is* in `AGENT_COMMAND_SET` (`src/api/scope.py`). It
  files under the caller's held task, so a session cannot create a phased
  graph (that needs root), and only the `planner` and `supervisor` profiles
  grant it.

**Playbooks V2 fan-out.** An `agent_task` step with
`wait_for_completion: false` dispatches a child and advances immediately with
outcome `dispatched` and `{task_id}` bound
(`src/playbooks/executors/agent_task.py:374-382`). A later `wait` of kind
`task` suspends on each child, and `ChildTaskReconciler` (in `engine.py`)
resumes it. Together these give **parallel agents plus a synthesis step**
inside one run, even though step execution is sequential.

- `pin_provider` on `AgentTaskStep` keeps a child on its model family.
- `llm` steps (`LlmStep`, `src/playbooks/definition.py`) can do cheap
  structured synthesis or judging.
- The only playbooks with an operator entry point are event-triggered or
  replayed with `aq playbook run` (`_cmd_run_playbook`,
  `src/commands/playbook_commands.py:230`), which prepares a *manual* event.
  Whether a non-task event can be replayed that way is unverified.

**Getting output from one agent to the next.** A downstream task's prime
renders its own `task_context` rows, its last 5 comments and handoff notes
(`build_task_context_section`, `src/prime/sections.py:333`). It does **not**
render the completion summaries of its dependencies. An ideation pipeline
therefore has to publish its artefacts somewhere the next agent is told to
read:

- a review document (`aq review submit`, which workers hold);
- task comments (`task_comment`/`task_comments`);
- a vault note. The `notes_*` commands exist
  (`src/commands/notes_commands.py`, "brainstorms, or analysis"), but no
  worker profile grants them.

**Roles and models.** Worker rungs are `<class>-<harness>`, so a mix such as
`deep-high-claude` (Fable) and `astra-high-codex` (Astra) is expressible per
node. Every worker rung inherits `needs_workspace: true` and
`workspaces: ["project-repo"]` from its template
(`src/profiles/defaults/worker-claude/profile.md`), so each ideation agent
takes a worktree slot. `read_only` does not skip acquisition
(`src/orchestrator/workspace.py:221`).

- **The "brainstorming skill"** is a harness-installed superpowers skill. It is
  not in this repo (a grep of `src/` finds no reference), and whether it exists
  in a worker's harness, especially `codex`, is **unverified**.
- **The worker `Task` harness tool** (sub-agents inside one session, with
  telemetry via `subagent_event`) is granted to Claude workers.

**Convergence output.** A roadmap submitted as `aq review submit --kind plan`
gets a vault file, a human decision, and a gate that later tasks can wait on
(`--after-review`) (`docs/guides/reviews.md`). This is the natural "artifact
plus approval" end state.

## 3. Gaps

1. **No starting verb.** Nothing turns "here is my brain dump" into "these N
   agents, these roles, this output". Today the operator has to write a graph
   or formula by hand, or rely on the supervisor improvising.
2. **No shipped template.** There are no default formulas and no example V2
   ideation playbook.
3. **No artefact passing.** Diverge outputs do not reach critique or converge
   agents unless a convention tells each agent where to write and read.
4. **Debate is round-based at best.** A static graph gives exactly one pass of
   diverge, critique and converge. Several rounds need a playbook, or live
   threads (see the [collaboration spec](2026-09-24-realtime-agent-collaboration.md)).
5. **Cost and footprint.** Every agent holds a repo worktree slot and a full
   worker prime (coding rules and completion protocol) that is irrelevant to
   ideation. There is no "thinking-only" profile.
6. **Brain-dump input.** It is unclear where the input lives: a task
   description (bounded by task field limits, unverified), a vault note, or a
   review. It is also unclear how every agent is told to read it.

## 4. Implementation options

### Option A — Shipped `mini-project` formula (phased graph on existing tasks)

`vault/formulas/mini-project.md` has vars `topic`, `brief_ref` (a vault path or
review id), `author_profile` (default `deep-high-claude`) and `critic_profile`
(default `astra-high-codex`). The phases are:

1. **diverge** — two or three nodes on different families, one told to "use
   the brainstorming skill if available"; each posts its proposal as a task
   comment or review draft at a named location.
2. **critique** — one node per proposal on the *opposite* family, reading
   siblings via `aq task children <epic>` and `task_comments`.
3. **converge** — one synthesis node that writes the roadmap and submits it
   with `aq review submit --kind plan`. Its close summary carries the review
   id.

The operator runs `aq formula cook mini-project --var topic=… --var brief_ref=…`,
or asks the supervisor to.

- **Touches:** one formula file, a default-formula seeding path (new, small,
  in the style of `vault.ensure_default_profiles`), and a doc page. No schema
  change.
- **Pros:** uses only shipped mechanisms. It is visible on the graph with
  phase progress bars. Tasks are durable and inspectable. Families are mixed
  per node.
- **Cons:** there is exactly one round, and artefact passing is a prompt
  convention. Every node pays for a worktree slot and a coding prime. It
  cannot run in `hierarchy`/`train` projects.
- **Size:** S–M.

### Option B — Example V2 playbook: parallel `agent_task` fan-out plus synthesis

Rule steps, in outline:

1. Proposers: `agent_task` × k with `wait_for_completion: false`, each on a
   different profile with `pin_provider: true`.
2. `wait task` on each proposer.
3. Critics: `agent_task` × k, inputs = proposer summaries.
4. `wait` on each critic.
5. `llm` step decides `converged | another_round` from a schema'd digest.
6. On `another_round`, loop back to a bounded `foreach` over rounds.
7. Synthesiser: an `agent_task` submits the roadmap review.
8. `wait human` (approve / revise).

The engine does support a loop, via a `foreach` over a literal round list
(the `literal`/`list` values are in `src/playbooks/expressions.py:110,146`).
Early exit is not possible because of `loop_body_escapes` (validation, around
`:1078`), so the body would skip work once converged. The trigger is the open
problem: a `task.created` filtered on a `mini-project` label or `task_type`
is plausible (`event_schemas.py:75`), but not verified end to end.

- **Touches:** a reviewed bundle (source, artifact, manifest), trigger design,
  and docs.
- **Pros:** multiple rounds and an explicit convergence test. Results flow
  between steps as bindings, so artefacts do not depend on prompt
  conventions. Receipts give an audit trail.
- **Cons:** playbook authoring and review are heavyweight for an ideation
  toy. Agent-task summaries are short strings, so rich proposals still need a
  document side channel. Wiring the trigger is awkward.
- **Size:** M.

### Option C — Single-task mini-project (harness sub-agents; how this doc set was made)

One task on a deep-class profile, with a description template such as "you
are the facilitator: fan out N sub-agents with roles X/Y/Z, collect, critique,
synthesise, submit the roadmap as a plan review". It is shipped as a skill
(`src/skills/aq-ideation/SKILL.md`) or a supervisor rule, and started with a
plain `aq task create`.

- **Touches:** one skill file and docs.
- **Pros:** the cheapest option, with one worktree slot. It is proven: this
  spec set is the demonstration. It needs no new mechanisms.
- **Cons:** one model family, because harness sub-agents run on the
  facilitator's model, unless the facilitator files AQ tasks for the other
  family. That needs `create_task`, which workers hold, but they file as
  children of the held task. Nothing is durable per sub-agent, and nothing is
  visible on the graph.
- **Size:** S.

### Option D — First-class `mini_project` entity

`aq mini-project start --brief <file> --agents 3 --rounds 2 --roles …` would
create a record (brief, members, roles, rounds, artefact), a shared thread, a
lightweight "thinker" profile family (no workspace, or `readonly-dir` only),
and a dashboard view that shows proposals side by side. It would probably be
built on B and grow into the [collaboration](2026-09-24-realtime-agent-collaboration.md)
entity.

- **Size:** L–XL.

## 5. Initial take

*Provisional.* Start with **C**, documenting the pattern that already worked,
plus a light version of **A** with one shipped formula. Together they give an
immediate path (C) and a durable, graph-visible, cross-family path (A)
without new mechanisms. The cross-family critique in A should reuse the
[adversarial review recipe](2026-09-24-adversarial-review-recipe.md)
vocabulary (`[blocking]`/`[nit]`, round cap), so the two features converge.

Promote to **B** only if one round turns out not to be enough in practice.
Consider **D** only after the collaboration work gives agents a cheap way to
wait on each other. Separately, add a thinking-only profile variant (no
`project-repo` requirement, or a read-only workspace kind) whichever option
wins. It removes most of the cost of "a few agents thinking".

## 6. Open questions

1. **Where does the brain dump live** (task description, vault note, review
   draft), and does every agent get it inlined (a `task_context` row, as
   formulas can already set `context:`) or by reference? This decides size
   limits and whether edits mid-session propagate.
2. **Where do intermediate artefacts live,** so that critics and the
   synthesiser find them? The candidates are task comments, per-node review
   drafts, or a vault notes directory (which needs a worker grant for
   `notes_*`). This decides the whole artefact-passing convention.
3. **One round or several?** If one, A and C suffice. If several, B, and the
   question becomes what the convergence test is (an LLM judge, a human, or
   the absence of `[blocking]` findings).
4. **Must a mini-project mix model families by default?** If yes, C is out
   except as a single-family quick path, and `pin` must be on each node.
5. **Is "the brainstorming skill" a requirement or a flavour?** It is not
   shipped by AQ. Should AQ vendor an ideation skill under `src/skills/` so
   that both harnesses have one?
6. **Who may start one:** only the operator and supervisor (A, because
   `formula_cook` is elevated-only), or any worker (C)? This decides whether
   agents can spawn ideation on their own initiative, which raises a cost
   question.
7. **What is the end state:** a `plan` review awaiting decision, or a direct
   task graph (the converge node proposes via `task_batch_propose`, which the
   default pipeline gates)? The second chains straight into execution, so the
   question is whether it should.
8. **Should a thinking-only agent need a workspace?** Critique grounded in
   code needs at least a read-only checkout. This decides whether a new
   workspace kind or profile is needed.

## 7. Dependencies and sequencing

- C and A have **no hard dependencies**. A needs a small default-formula
  seeding path.
- The critique phase should follow the
  [adversarial review recipe](2026-09-24-adversarial-review-recipe.md)
  conventions, so settle that recipe first.
- A live multi-round debate (the D direction) depends on the
  [collaboration](2026-09-24-realtime-agent-collaboration.md) spec's wait
  primitive and on [agent sleep/wake](2026-09-24-agent-sleep-wake.md).
- The [resource-aware planner](2026-09-24-resource-aware-planner.md) may want
  to treat ideation tasks as cheap or no-workspace work when sizing.

## 8. Non-goals

- Replacing the planner or spec-ingest pipelines. A mini-project ends in a
  roadmap for a human decision, not in executed work.
- Real-time chat between the agents, which belongs to the collaboration spec.
- A general multi-agent debate framework, voting, or ensembles.
- Automatically triggering ideation from Discord or voice input (see the
  sibling [Discord specs](2026-09-24-discord-voice-transcripts.md)). The input
  path is out of scope here.
