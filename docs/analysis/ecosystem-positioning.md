---
tags: [analysis, positioning, strategy, ecosystem, flywheel]
date: 2026-08-20
status: opinion — written to be argued with, not filed
---

# Ecosystem Positioning — agent-queue vs. the Agent Flywheel

**What this is.** A decision document for the project owner, grounded in seven investigation
reports on the Agent Flywheel collection (~39 tools, almost all by Jeffrey Emanuel /
`Dicklesworthstone`) and re-verified against this repo's source. It answers one question —
*how should agent-queue position itself, and where does it provide real value over that stack?*
— and does not answer "which of these 39 tools should we adopt," because after the analysis
that question turns out to be nearly empty.

**Method note.** The seven reports are good but secondhand. Where a claim was load-bearing I
checked it against source; those checks are marked **[verified]** and cite files. Two findings
below contradict our own specs as a result.

---

## 1. The positioning answer

The thesis I was asked to attack was:

> The flywheel is a toolbox for a human operator on one machine; agent-queue is a headless
> multi-project daemon. Different categories. Our differentiators are transactional coherence,
> multi-project scoping, and a correctness posture about agents lying. Strategy: own the core,
> consume the edges, steal the algorithms. Uncomfortable part: for one dev with one repo, the
> flywheel is probably better today.

**Roughly a third of that is wrong, a third is right but sold under the wrong name, and a
third is right.** Corrected version:

> **agent-queue is the thing that answers "what should run next, and why isn't it running?"
> across dependencies, gates, workspace locks, caps and budget at once — and then acts on the
> answer without a human in the loop. The flywheel has no component that answers that question,
> because in the flywheel the answer is the operator.**
>
> That is the whole product. Multi-project is a *multiplier* on it, not a differentiator beside
> it. "Transactional coherence" is not a differentiator at all — it is the implementation
> technique that makes the cross-domain readiness predicate cheap and non-drifting, and it is
> also the reason two of our subsystems are currently paused. The genuinely day-one
> differentiator, the only one that shows up for a solo dev with one repo, is that **process
> exit carries no information about task success**, and agent-queue is built around that fact
> while the flywheel is built around a human eyeballing a pane.
>
> The competitive boundary is not "human vs. headless." It is **who decides a unit of work is
> done**. Everything else follows.

### Why the human-operator/daemon distinction is wrong as stated

It is wrong about the *assembled* flywheel even though it is right about each individual tool.
`ntm serve` ships REST + SSE + WebSocket + OpenAPI. Mail runs an MCP server over HTTP with
durable delivery cursors and a Git-backed audit ledger. `br` has an optional MCP mode. CASS and
EE are queryable indexes with `--robot` JSON. WA's `frankenterm-mux-server` is explicitly a
headless mux for driving a remote fleet from a laptop. A competent operator can drive that stack
from a shell script on a box they never look at. NTM even has a work graph (via `br`), file-edit
reservations, an approval system, checkpoints, and worktree-per-agent. Calling that "a toolbox
for a human operator" flatters us.

What NTM genuinely does not have, and this is the line worth defending:

- **No completion protocol.** A human or the agent's own vibe decides when a pane's work is
  done. There is no `aq task close --outcome … --failure-class …` contract, no drain-ack, and
  no exit classifier that treats a dead pane as a failure signal rather than silence.
- **No task queue with a scheduler.** Nothing decides *which* work starts next; the operator
  spawns panes.
- **No cross-domain readiness predicate.** `br ready` knows about edges. It knows nothing about
  whether a worktree slot is free, whether the project is over budget, whether a PR gate is
  open, or whether the profile is in provider cooldown. Those live in four different tools, or
  in the operator's head.

Use *those* three sentences as the competitive statement. Drop "headless vs. attended" — it is
not true and it invites a rebuttal you will lose.

### Why "transactional coherence" is the wrong flag to plant

It is an engineer's aesthetic dressed as a user benefit, and I think you know that. Nobody has
ever wanted one migration chain. Three specific problems with it as a pitch:

1. **Sidecars can be correct.** Mail's `UPDATE … SET read_ts = COALESCE(read_ts, ?)` inside an
   MVCC-retry transaction is exactly as strong a compare-and-set as our `WHERE … IS NULL`. `br`
   does atomic admission control inside `BEGIN IMMEDIATE`. The reports establish that these
   people know how to write correct transactions. "They'd lose atomicity" is false; what they
   lose is atomicity *across domains*.

2. **We don't actually have it either — partly by design, partly not.** By design:
   `work-graph.md` §4.1 is explicit that `is_blocked` is *graph* blockedness only — "transient
   capacity reasons — no idle agent, workspace locked, budget, cooldown — are **not**
   persisted." So `aq task explain` already merges a persisted projection with live per-tick
   scheduler state. Not by design: **[verified]** multi-kind workspace acquisition is
   *compensating rollback*, not one transaction — `acquire_for_task`
   (`src/orchestrator/workspace_attachments.py:98`) takes each per-kind lock in its own
   transaction and unwinds the acquired ones in an `except` block; its own docstring says so.
   And **[verified]** `provider_cooldown` state is a plain dict on the Orchestrator instance
   (`src/orchestrator/core.py:270`) — no table, cleared by every daemon restart.

   So the unified predicate is assembled at read time from a persisted projection, a
   compensating-rollback lock protocol and an in-memory dict. The transaction is emphatically
   not what makes `explain` possible; **the shared schema is what makes the join cheap.** Only
   the second claim survives, and it is the one to make.

3. **It has a price you are currently paying.** Every new capability has to land in the same
   32-table schema and the same linear Alembic chain, which is precisely why the execution plan
   needs a "substrate rule" landing all eight config dataclasses and every new table serially in
   Wave 0 before five agents can work in parallel. Coherence is why memory and playbooks were
   cheaper to pause than to keep alive. It is a real cost, it bought a real thing, and the pitch
   should say so.

**Keep the property. Change the claim.** The claim is: *one query answers "is this runnable,"
and no two components can disagree about it.* That is user-visible (it is `explain`, it is the
ready frontier, it is one backup and one restore) and it is defensible.

### Why multi-project is real but second-order

It is not a niche — it is just not a *count* argument. If it were "N repos," it would be weak:
most solo devs have one active repo. What makes it load-bearing is that `project_id` is the
scoping dimension for profiles, MCP registries, workspace kinds, budgets, concurrency caps,
vault memory, Discord channels *and* fair-share credit weight simultaneously. Gas City has the
same idea (rigs) and refuses cross-store routing; we allow cross-project edges as ordinary rows
because it's one DB. The flywheel lacks it not because it's niche but because a pane has no
project.

So: multi-project is the same argument as §1 wearing a different hat. It multiplies the value
of the readiness predicate. Cross-project *dependency edges* specifically are genuinely rare and
should not be marketed. Do not lead with multi-project.

### Why the "correctness posture" is the strongest of the three, and misnamed

"Agents lying" is the wrong frame — it is moralizing about a plain engineering fact. The fact:
**a process exiting tells you nothing about whether the work succeeded.** Everything downstream
follows deterministically from taking that seriously — explicit close with a typed outcome, an
exit classifier, adopt-don't-reset on daemon restart, gates as records, `failure_class`-driven
retry.

This is also the *only* differentiator that turns on for one dev with one repo on day one. Run
six agents overnight; two hit a rate limit at 3am. The flywheel's answer is "look at your panes
in the morning." Ours is `PAUSED(rate_limit)` with `resume_after` and an automatic resume. WA's
Bayesian change-point stall detection is the flywheel's attempt at exactly this problem and it
is the most interesting engineering in the collection — which tells you the problem is real and
they feel it.

One honest deduction: **drain-ack specifically is the weakest link in the chain.** If the agent
forgets to call it, you fall through to the exit classifier anyway, so its marginal value over
"explicit close + classifier" is small. Its real justification is narrow — the daemon needs a
window to deliver final messages and flush the ledger before killing the session. Keep it, but
don't put it in the pitch; put the exit classifier in the pitch.

### Is watching a pane just... fine for most people?

For most people, yes. That is not a defeat; it is a market statement. Watching a pane is fine
until one of four things is true: you are asleep, you have more than one project, you need to
know what happened last Tuesday, or the machine rebooted. agent-queue's entire value is
conditional on at least one of those. If none of them is true for you, you should be using tmux
and a text file, and we should say so out loud rather than pretending otherwise.

---

## 2. What agent-queue must own and never delegate

Each with the reason it is load-bearing, not just "it's core."

| Own | Why it cannot be delegated |
|---|---|
| **`tasks` + `task_dependencies` + `is_blocked`** | Not because of transactions — because the ready frontier is a join across four domains that only exist together here. A sidecar tracker knows edges and nothing else; the join is the product. |
| **`gates` + `task_gates`** | `br`'s "gate" is a status-transition approval verdict; ours is a blocking wait record with `timer`/`pr-merged`/`ci-run`/`event`/`task` types. Nothing in the flywheel has this shape. It is the single mechanism behind human approval, PR merge, CI, timers and `aq ask` — collapsing three wait statuses onto it (work-graph §12) is only possible because it's ours. |
| **`sessions` + the exit classifier** | This is the differentiator (§1). The classifier's verdicts (`PAUSED(rate_limit)`, restart-with-backoff, quarantine after `max_restarts`) are the thing that makes unattended operation mean anything. Delegating it means delegating the product. |
| **The completion protocol (`aq task close --outcome/--failure-class/--work-outcome`)** | It is the typed input to retry policy today and to reflection later. A free-text "done" — which is what `br close --reason` and every flywheel tool offers — cannot drive `transient` vs `hard` retry. |
| **Workspace acquisition + merge slot** | All-or-nothing multi-kind acquisition in canonical lock order is a deadlock-freedom property. Split it across a second datastore and you have a distributed transaction with no coordinator. The merge slot as a *DB row with a lease* (rather than a PG advisory lock) is deliberately survivable across daemon restarts. |
| **`messages` + the delivery engine** | Mail is genuinely richer as a *message model* (CC/BCC, attachments, separate read/ack). It is also **pull-only** — no wake-on-message, no idle-nudge, no busy-inject. The hard half of the problem is ours and unbuilt; the easy half is theirs and built. Adopting Mail buys the easy half at the cost of a second datastore. |
| **The scheduler's admission decision** | Currently a heuristic, not an invariant — see §5.1. This must become an invariant, and an invariant cannot live in another process. |
| **The vault as source of truth** | Principle #1. Every flywheel storage design (CM's YAML playbook, EE/CASS/FSFS's SQLite+index, MS's dual SQLite+Git) treats derived storage as canonical and markdown as export. Ours is the reverse and that is the reason a human can fix what the system learned wrong. |

**Sobering caveat on that table, verified against source.** Three of the eight rows describe
things that are *right to own* and *not yet built*:

- **Gates are schema plus one read clause.** `gates`/`task_gates` exist
  (`src/database/tables.py:182,209`), the blocked predicate reads them
  (`blocked_state.py:176-189`), and that is all. `_sweep_gates`
  (`src/orchestrator/core.py:2093-2113`) is a flag check and a `return`; `GateCommandsMixin`
  (`src/commands/gate_commands.py`) is an empty class body; no `create_gate` / `resolve_gate`
  exists anywhere. Discord already registers a `/gates` slash command that calls a command that
  does not exist. **A gate can block a task only if you insert the row by hand.**
- **Merge slots are a table and a dataclass.** No acquire, no release, no lease-break. The only
  reference to `break_expired_merge_slots` in the tree is the docstring of a stub.
- **The tmux provider is not a file.** `src/sessions/tmux.py` does not exist;
  `default_session_registry` imports it inside a `try/except ImportError` that always fails.

And three complete subsystems ship dark: `work_graph.blocked_state_authoritative`,
`sessions.enabled`, and `worktrees.enabled` all default `False`. For `is_blocked` specifically
that means the projection is computed correctly, compared against the legacy scan, divergence
is logged — and **the legacy scan still decides** (`src/orchestrator/monitoring.py:85-98`).

None of this changes what we should own. All of it changes how loudly we can currently claim the
readiness predicate as a differentiator, and it is the evidence base for §9.

Everything else is negotiable.

---

## 3. What agent-queue should deliberately NOT build

Named specifically, including things currently on the roadmap.

1. **Milvus, or any vector-DB dependency, when memory un-pauses.** `memory-plugin.md` still
   assumes a Milvus backend and `feature-pauses.md` §9 gates the comeback on it. The Milvus
   dependency is a substantial part of why memory was expensive enough to pause. The comeback
   should be **SQLite FTS5 first** — zero new moving parts, same DB, principle #10 — with an
   embedded index (Model2Vec-class, CPU, no server, no API key) as an optional second tier. This
   is a roadmap change, not a "someday."
   *Do not* swap Milvus for FSFS-as-subprocess: you would trade one server for a
   subprocess-per-query in an async daemon, which is the same amount of integration work for a
   worse latency story.

2. **Graph-centrality ranking of the ready frontier.** `bv`'s PageRank/betweenness/HITS/critical
   path is the most seductive item in all 39 tools and the least useful to us. Our frontier is
   `(priority, created_at)` and on a real solo install the frontier has three items. Centrality
   ranking pays off at 300 open tasks. Revisit when a live install crosses ~100 open tasks;
   until then it is a science project.

3. **A destructive-command classifier.** See §4 on SLB/DCG. Building one inside `gates` is
   technically easy and strategically wrong for us.

4. **Cross-provider session conversion (CASR-style).** Nothing in any spec wants it. Resuming a
   stuck Claude session as a Codex session is a human's debugging move, not a reconciler action.

5. **A second knowledge/skill index (MS-style, or a hosted prompt library).** Vault + the prime
   renderer already occupy that niche. Adding a Rust-backed SQLite+Git skill store with its own
   MCP server next to the vault violates #1 and #10 at once.

6. **Statistical stall detection (WA-style Bayesian change-point over pane text).** The stall
   ladder is deterministic and every verdict is explainable in `aq task explain`. A model whose
   output you cannot render as a reason code is not an upgrade. The *one* idea worth keeping in
   the back pocket: stall detection should probably have hysteresis rather than a single
   threshold — but only if the deterministic ladder proves noisy in practice.

7. **A disk-pressure daemon (SBH-style EWMA + PID + ballast files).** `aq doctor` has no
   volume-level free-space check today. Write the ten-line `disk.free_space` check. That is 80%
   of the value at 0.5% of the machinery.

8. **Any pane-text inference beyond readiness, startup dialogs, and nudge-submit confirmation.**
   This is already the session-runtime design rule. Worth restating because the flywheel's most
   impressive engineering (WA) is entirely on the wrong side of it, and impressive engineering
   is persuasive.

9. **Token-format compression (TOON/TRU).** Prime output is prose, not JSON; the published
   accuracy evidence cuts both ways and is model-dependent; and `aq-surface`'s actual answer to
   the same token bill is architectural (shrink the MCP tool surface from ~127 to ~8) rather
   than a codec. Architectural fix already chosen; don't add a second one.

10. **Process triage / network observation / resource protection daemons (PT, RANO, SRPS).**
    These compete with `aq doctor`'s deterministic checks and lose on our own stated philosophy.
    Document them as optional operator tooling if you like them personally.

---

## 4. Resolving the six specific tensions

### 4.1 Mail's TTL file reservations vs. our worktree isolation

**Structural isolation is strictly better at what it does, and it does not do the thing Mail
does.** Worktrees prevent two agents from editing the same file *at the same time*. They do
nothing about two agents editing the same file *in different branches* — that conflict is
deferred, not prevented, and it surfaces at the merge slot where it is maximally expensive
(the work is done, the rebase fails, the task goes `needs_attention`).

So intent-signaling does matter once a merge slot serializes integration. But **not as a lock** —
as a *scheduling input*. Mail's answer (TTL leases + a bypassable pre-commit hook) is the wrong
mechanism for us: it's advisory, it's cross-process, and it needs a second datastore.

**The cheap version, which I think is the single best new idea in this document:** the
worktree spec *specifies* a `merge.conflict` event carrying the conflicting **file list** (§8) —
**[verified]** as spec only; the merge slot that would emit it is schema-and-dataclass with no
serializer, so nothing fires it yet. When it is built, consume it. Store a `touches:<path-glob>`
hint in `task_metadata` (metadata-first rule, no migration),
and make the scheduler treat overlapping `touches` sets as *soft anti-affinity* — prefer not to
dispatch two tasks with overlapping footprints concurrently, and record `conflict_risk` as an
explain reason when it withholds. Seed the hints from the supervisor's task-graph creation
(it knows what it's asking for) and refine them from actual conflict events. That is a closed
learning loop that costs one metadata key and one scheduler predicate, needs no new table, and
is exactly the kind of thing reflection was supposed to feed.

**Also worth naming while we're here:** the bigger parallelism loss isn't file conflicts, it's
the documented one — plan subtasks share the parent's branch, git allows one checkout per
branch, so **a parallel plan executes serially** (worktree-execution §4.4). That's already
accepted-with-reasons and correctly deferred to Phase 3, but it dwarfs the conflict problem.

### 4.2 SLB's real carve-out: effects outside the worktree

**Yes, it is a genuine hole, and no, it does not argue for a classifier inside `gates`.** Split
the hole in two, because the halves have different answers:

- **Cloud/infra destruction** (`terraform destroy`, `kubectl delete namespace`, `DROP DATABASE`,
  `aws terminate-instances`). These require *credentials*. `trust-and-ops.md` §3 already
  withholds the daemon's own secrets by default and passes only the harness credential
  allowlist. **An agent whose profile was never given cloud credentials cannot destroy cloud
  state**, regardless of what it types. The hole is real only for profiles the operator
  deliberately grants infra credentials to — which is a small, enumerable, operator-chosen set.
  The correct fix is a **spec sentence, not code**: any profile granted infra credentials must
  declare its destructive operations behind a `human` gate. Credential scoping is a real
  boundary; a command classifier is not.

- **Host-level destruction that needs no credentials** (`rm -rf ~`, `dd of=/dev/sda`, `sudo`
  anything). This is genuinely unaddressed and a classifier would not address it either — it
  would be exactly the theater `trust-and-ops` §2.5 rejects. The only real control is OS-level
  (a container, or running the daemon as an unprivileged user whose home contains nothing but
  the repo bases), and §10 already declares sandboxing a non-goal for this phase. **The honest
  action is to move this from an unstated gap to a stated one**: add a row to §4 "Honest limits"
  saying so, and add the one cheap mitigation that is not theater — run the daemon as a
  dedicated user. That costs a paragraph in the install docs.

If you want *something* mechanical: an opt-in, operator-authored `gate_commands: [...]` list in
a profile, matched as literal command prefixes, raising a `human` gate through the existing
primitive. ~50 lines, no new concepts, claims nothing it can't deliver. **Ranked low** — do it
only after a real incident.

### 4.3 DCG failing open, and `DCG_BYPASS=1`

**DCG is philosophically consistent with our position, and should still not be a dependency —
for a reason the reports don't reach.**

Consistent: `trust-and-ops.md` §3 makes DCG's exact argument on our own behalf, about our own
env-var denylist — *"this denylist is best-effort, not complete… the difference is the input,
not the technique."* Our spec rejected the *claim* that pattern matching is a security boundary,
not the *technique* of catching honest mistakes with patterns. DCG's README says it catches
"honest mistakes, not adversarial attacks" — it makes exactly the concession our spec demands.
Deploying it does not require accepting reasoning we rejected.

Not a dependency, for a coherence reason: **DCG is a harness hook** (`PreToolUse`). We already
own the harness hook budget — `SessionStart → aq prime`, `PreCompact → aq handoff`,
`UserPromptSubmit → aq inbox`, all merged into a generated `--settings` file. A fourth hook from
a third party means our settings merge has to compose with theirs, and worse: **a DCG deny is
invisible to the daemon.** No event, no task metadata, no gate, no explain reason. You'd get
tasks failing for reasons the orchestrator cannot narrate — the precise failure mode this entire
strategy exists to avoid.

**Verdict:** recommend it in the docs as an operator-installed, personal, defense-in-depth tool
for the daemon host, explicitly outside the daemon's hook budget and explicitly not depended on.
Add one sentence to `trust-and-ops.md` §4 "Honest limits" pointing at it. Never wire it in.

### 4.4 CAAM's Shallow Profiles

**The most genuinely new capability the seven reports found, and the tool is still the wrong way
to get it.**

New ground, confirmed: our `provider_cooldown` explain code knows exactly one credential set per
profile. Rotating N subscription accounts across N concurrent sessions is not a duplicate of
anything we have.

Three reasons not to adopt the binary: (a) it holds **plaintext** OAuth bearer tokens for every
account behind `0600` perms — today agent-queue custodies *no* vendor credentials at all, it
passes the operator's own through, and that is a materially better posture we would be giving
up; (b) it inverts the trust model, gating session spawn on a third-party binary's state, which
`trust-and-ops.md` §2.1's "operator-authored" taxonomy has no category for; (c) multi-account
rotation to route around usage limits is at minimum unresolved against provider ToS, and that
is a larger practical risk than the license rider.

**Take the shape, not the tool.** The shape is: a synthetic `$HOME` per identity where only the
provider's auth files are real and everything else symlinks back. Our `SessionSpec` builder
already constructs the child environment from scratch — that is the whole point of `scrub_env`.
Adding an optional `credential_profile: <name>` to the agent profile, which sets `HOME` /
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` to an operator-prepared directory, is roughly thirty lines,
custodies nothing (the operator ran `/login` in each directory themselves), and slots straight
into the existing cooldown model: **`provider_cooldown` becomes keyed per credential profile
rather than per profile**, so a rate-limited account withholds only the sessions bound to it.

Reserve the field in `session-runtime`'s `SessionSpec` now; implement when someone actually has
two accounts. Document the ToS caveat next to it.

### 4.5 CAUT and the shortest path to a fed `token_ledger`

The reports concluded CAUT validates the technique but its output has no `task_id`, so the
transcript readers remain the first writer. **That conclusion is correct about CAUT and wrong
about the shortest path, and our own spec is wrong the same way.**

**[verified]** `src/database/tables.py:252-267` — `token_ledger` already carries nullable
`model`, `input_tokens`, `output_tokens` alongside `tokens_used`. No writer populates them.

**[verified]** `src/runtimes/claude_sdk.py:655-658` —

```python
usage = getattr(message, "usage", None)
if usage and isinstance(usage, dict):
    tokens_used += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
```

**[verified]** `src/runtimes/acpx.py:240-253` — the same, normalising both `inputTokens` and
`input_tokens` shapes, then summing.

**Both runtimes already have the split and throw it away**, because `AgentOutput`
(`src/models.py:850-867`) has one `tokens_used: int` field and nowhere to put it.

**[verified]** and this is the part that makes it near-free: `record_token_usage`
(`src/database/queries/token_queries.py:17`) **already accepts `model`, `input_tokens` and
`output_tokens` as keyword arguments**. The live writer at `src/orchestrator/execution.py:911`
simply never passes them. So the shortest path is not transcript readers and needs no tmux, no
WSL, no Wave 2-T:

> Add `model: str | None`, `input_tokens: int | None`, `output_tokens: int | None` to
> `AgentOutput`; stop collapsing the split at the two capture sites above; pass them at the two
> existing call sites (`src/orchestrator/execution.py:911`,
> `src/orchestrator/sync_workflow.py:324`).

Three files, no schema change, no migration. It makes `aq costs` produce non-zero numbers on a
real install immediately, and it makes transcript readers the *second* writer (for tmux
sessions) rather than the first. This is the highest value-per-line item in the entire document.

**Correct `trust-and-ops.md` §7**, which currently states "the transcript readers from
[[session-runtime]] are the first writer that populates model + split." That is only true if we
choose not to widen `AgentOutput`, and widening it is strictly cheaper than the alternative it
recommends.

### 4.6 What beads-rust's independent convergence tells us

`br`'s `DependencyType` — `Blocks, ParentChild, ConditionalBlocks, WaitsFor` blocking;
`Related, DiscoveredFrom, Duplicates, Supersedes` non-blocking — is our taxonomy. Two
independent implementations landing on the same eight edge types means one thing:

**The edge taxonomy is commodity. It is not a differentiator and never was. Stop designing it.**
Ship WG-2 exactly as specced and spend zero further hours there. `br`'s extras (`replies-to`,
`caused-by`, `Custom(String)`) are not worth chasing; our CHECK constraint plus the metadata-first
rule is the better default.

The convergence also localises where effort *is* worth spending: `br` is ahead in exactly one
place, **atomic admission control** — hierarchy-aware counting over `parent-child` edges,
per-actor/per-harness/per-subtree admission scopes, and the cap check committed inside the same
`BEGIN IMMEDIATE` as the status write. That is also the place our scheduler is genuinely thin
(§5.1). When two independent designs converge on the substrate and diverge on the scheduler,
the scheduler is where the remaining design risk lives.

---

## 5. Algorithms worth stealing, with concrete code impact

Ranked. Each is *read-for-ideas* — the lowest possible license exposure (see §7).

### 5.1 Atomic admission control (`br`) — **highest value**

**[verified]** `Scheduler.schedule(state)` (`src/scheduler.py`) is a pure function over a
`SchedulerState` snapshot; caps are enforced by counting against `round_agent_counts` inside the
snapshot (`src/scheduler.py:360-390`), and assignment writes happen afterwards in
`Orchestrator._schedule()` (`src/orchestrator/core.py:2180-2301`).

Today this is safe, because there is exactly one writer: the single asyncio loop. **It is a
heuristic, not an invariant.** It breaks the moment there is a second admitting writer — and the
overhaul is deliberately creating one: a supervisor agent that calls `aq task create --graph`,
`aq task assign`, and friends over REST while the cascade is running. `max_concurrent_agents`
will silently become a suggestion.

**What to change:** make admission a conditional `UPDATE` in the same transaction as the
assignment write (`UPDATE … WHERE (SELECT count(*) FROM agents WHERE project_id = ? AND status =
'BUSY') < ?`), so the cap is enforced at write time, not snapshot time. Keep the deficit
function pure — it stays the *ordering* policy; the cap becomes an *invariant*. Add `br`'s
hierarchy-aware counting so pure container tasks (`parent-child` parents that never execute)
don't consume slots.

Add to Wave 3. It is currently nowhere on the roadmap.

### 5.2 Shallow HOME profiles (CAAM) — see §4.4

Reserve `credential_profile` on the profile and in `SessionSpec` now; key `provider_cooldown` by
it when implemented. **[verified]** while you are in there: cooldown state is an in-memory dict
on the Orchestrator (`src/orchestrator/core.py:270`, written at
`src/orchestrator/execution.py:1508`), so **every daemon restart forgets every rate-limit
cooldown** and the next tick will happily dispatch straight back into the wall. For a system
whose pitch is crash survival that is an embarrassing little hole, and it is a two-column table
or a `system_config` key to close.

### 5.3 Merge-conflict → scheduling anti-affinity (synthesised from Mail) — see §4.1

`touches:` metadata hint, soft anti-affinity in the scheduler, `conflict_risk` explain code, fed
by the `merge.conflict` event payload once the merge slot actually emits one. Cheap,
closed-loop, novel — and it lands naturally alongside the merge-slot work rather than adding a
separate workstream.

### 5.4 Operating envelope / shed (WA) — the honest version

WA's planner returns `envelope.shed` when pane count exceeds what live resource pressure
justifies. We schedule against config caps only, with no notion of the host. The honest,
non-Bayesian version: one `disk.free_space` doctor check plus a load/free-space gate in the
scheduler that withholds dispatch with a new explain code `host_pressure`. That single reason
code makes SBH, SRPS and PT all unnecessary and is maybe forty lines.

### 5.5 PFR's pre-boot mtime death-cluster — keep on the shelf

Genuinely non-obvious: distinguish "died in the power cut" from "idle since Tuesday" by
clustering session-file mtimes just before recorded boot time. Only relevant when the *host*
died, not the daemon — which our `sessions` table plus adoption already covers. If it ever
matters, it is a **doctor check** (`sessions.orphaned_by_host_crash`), never a cascade step.

### 5.6 DCG's tiered matching architecture — reference only

Fast `RegexSet` reject → extract nested `bash -c`/heredoc content → AST match via tree-sitter,
recursing into nested shell. If §4.2's opt-in `gate_commands` ever gets built, this is how you'd
build it properly. Do not build it now.

**Explicitly not stolen:** bv's centrality ranking (§3.2), WA's Bayesian stall detection (§3.6),
PT's 40-model ensemble (§3.10), TOON (§3.9).

---

## 6. Concrete adoption candidates, ranked

"Adoption" here means anything from reading a README to taking a dependency. Note how little of
this list is a dependency — that is the finding, not an accident.

| # | Item | Shape | Value | License exposure |
|---|---|---|---|---|
| 1 | CAAM's shallow-profile pattern | **read-for-ideas**, implement natively | High — closes a real capability gap in cooldown | None |
| 2 | `br`'s atomic admission control | **read-for-ideas** | High — closes a real invariant gap | None |
| 3 | CASR's session-format knowledge (Codex, Gemini, Kiro, Cline's `state.vscdb`) | **read-for-ideas** when writing `codex.py`/`gemini.py` readers | Medium — saves format-archaeology | None for reading; **contingent** if their golden fixture files are copied into our tests |
| 4 | DCG on the daemon host | **operator installs; documented; not wired** | Medium — real mistake-catcher, zero coupling | Low (operator's own act, our repo references nothing) |
| 5 | Pi Agent Rust as a fifth harness | **harness-as-markdown; operator installs binary** | Medium — only path to local/offline models | Medium (daemon `exec`s it — textually "executing… in a pipeline for automated systems") |
| 6 | UBS augmenting `aq-vibecop` | **shell-out from an optional plugin** | Low-medium — Go/Rust/Java coverage vibecop lacks | Medium (same as #5) |
| 7 | MDWB / MS as registered MCP servers | **MCP register** — already supported by `mcp_registry.py`, so this is a docs act | Low | Medium |
| 8 | FSFS replacing Milvus for L3 | **rejected** — prefer SQLite FTS5 (§3.1) | — | — |
| 9 | `bv` graph ranking | **deferred** — revisit >100 open tasks | — | — |
| 10 | Mail replacing `messages` | **rejected** — take the `touches:` idea instead (§4.1) | — | — |

Two things this table says out loud:

- **Everything above the fold is "read-for-ideas."** Nothing genuinely valuable is a dependency.
- **The Windows problem is structural.** Five of the six ops tools are Linux-or-WSL2 only; NTM
  and WA have no Windows support at all. The daemon targets Linux/WSL2 anyway, but the dev
  machine is Windows and WSL2 cannot currently boot (§8). Any "just shell out to it" plan is
  untestable on the machine where the work happens.

---

## 7. The license question — what it does and does not gate

I am not resolving this and neither can you; the useful output is knowing which options depend
on the answer.

The rider (in every `Dicklesworthstone/*` repo checked across all seven reports; **not** in Power
Failure Resumer, which is plain MIT; Rust Proxy has *no* license, which is worse) denies all
rights to "Restricted Parties" — OpenAI, Anthropic PBC, affiliates, and anyone acting "on behalf
of, for the benefit of, or under the direction of" them — with "use" defined to include
executing, benchmarking, testing, analyzing, indexing, and incorporating into "any… pipeline for
machine learning or other automated systems."

**My two prior agents disagreed on reach. That disagreement is itself the finding:** one read it
as plausibly biting an agent-queue deployment, one read it as targeting Anthropic
staff/contractors rather than ordinary customers. Ambiguity, not a verdict.

Exposure ordered lowest to highest:

| Option | Exposure | Note |
|---|---|---|
| Reading a public README/source to understand a design | **Effectively none** | A license grants rights you would otherwise lack. Reading a public repo needs no grant, and *ideas* are not copyrightable. The mechanism by which a rider would bite reading-for-ideas is unclear; the mechanism by which it bites vendoring is obvious. |
| Independently reimplementing an algorithm from its description | **Effectively none** | Same reasoning. This is where every item in §5 sits. |
| Recommending the operator install a tool themselves | **Low** | The act is theirs, our repo ships nothing. |
| Daemon shells out to the binary as a documented optional dependency | **Medium** | This is textually squarely inside the enumerated "use" list — *executing*, in a *pipeline for automated systems*. Options #5, #6, #7. |
| Vendoring source into this repo | **High** | Copying and distribution, unambiguously. |
| Redistributing (shipping in an installer, a Docker image, a vault pack) | **Highest** | Don't. |

**The strategic point: this never has to be resolved.** Everything worth having is in the top
two rows. Every dependency-shaped option is also, independently, low-value. The license is
therefore a tiebreaker that never gets to break a tie — which is a much better outcome than
either legal answer would have been.

One practical note if any medium-exposure option is ever taken: GitHub's own detector returns
`NOASSERTION` for these repos, so they will not clear automated license-compliance scanning
without a manual exception.

---

## 8. Roadmap implications

**Mostly: this changes nothing, and that is the finding.** Wave 2's scope is correct, the design
specs hold up well against seven independent implementations of adjacent problems, and the
convergence with `br` is evidence the substrate design is right rather than a reason to revisit
it.

**But the verification pass changed the priority ordering, and that matters more than any
adoption decision here.** Three complete subsystems ship dark —
`work_graph.blocked_state_authoritative`, `sessions.enabled`, `worktrees.enabled` all default
`False` — and three specified subsystems are schema-only (gates, merge slot, tmux provider).
The distance between the specs and a running system is larger than the distance between
agent-queue and anything in the flywheel. **Nothing in this document should be scheduled ahead
of turning the dark flags on and filling the empty mixins.** Six deltas, in order:

1. **Wave 2-T's priority goes *up*, not down — it is the single highest-ROI item on this
   entire list and it costs a reboot.** The execution plan records that WSL2 cannot boot because
   `VirtualizationFirmwareEnabled: False` (SVM disabled in BIOS). Every honest concession in
   §9 below traces to the same root: agent-queue cannot currently run a real session on the
   machine where it is being built, so the C1 checkpoint targets a **fake** provider. The gap
   between agent-queue and the flywheel today is *completion*, not architecture, and the first
   gate on completion is a BIOS setting. Do that before anything else in this document.

2. **Add one small lane: widen `AgentOutput` with `model`/`input_tokens`/`output_tokens`** and
   pass them at the two existing call sites (§4.5). Three files, no migration, needs no tmux and
   no WSL. Amend `trust-and-ops.md` §7, which currently names the wrong first writer.

2b. **Persist `provider_cooldown`** (§5.2). Currently lost on every restart. Small.

3. **Add atomic admission control to Wave 3** (§5.1). It is currently on no list, and the
   supervisor agent is about to become the second admitting writer that makes it necessary.

4. **Re-open the memory comeback's backend as a decision** (§3.1): SQLite FTS5 first, not Milvus.
   That is an edit to `feature-pauses.md` §9's un-pause criteria, not a new workstream.

5. **Reserve `credential_profile` in `SessionSpec`** now (§4.4). Cheap; keys `provider_cooldown`
   correctly when it lands.

Two things to add for hygiene, both one-liners: the `disk.free_space` doctor check (§3.7), and a
`trust-and-ops.md` §4 row admitting the host-level destruction gap plus the "run the daemon as a
dedicated unprivileged user" mitigation (§4.2).

**Do not add:** harness count. The `claude → codex → gemini → opencode` rollout is sufficient;
Pi is optional and its resume-by-id story is unverified (its `-r` opens an interactive picker,
which is exactly where the harness-profile abstraction leaks — at the restart-with-resume path
that matters most). Harness breadth is cheap in the common case and expensive precisely at the
failure paths; do not let "14+ agents supported" become a goal.

---

## 9. The strongest argument against agent-queue existing at all

Stated as well as I can make it:

> You are one person with one machine. The problem you actually have is *"I want more than one
> agent working at once without babysitting them."* That problem is solved today by tmux and a
> text file, and better than you will solve it in six months.
>
> Every capability agent-queue offers beyond that has a cost you personally pay: thirty-two
> tables and twenty-eight migrations that must stay green; an execution plan that needs a
> "substrate rule" landing every table and config dataclass serially before five agents can work
> in parallel; two entire subsystems — memory and playbooks, roughly twelve thousand lines —
> switched off because keeping them alive cost more than they returned; a daemon that does not
> run on your own machine because a BIOS flag is off; and a Wave 2 whose success checkpoint is a
> *fake* session provider.
>
> And look at what "Wave 2 merged" actually bought. `gates` is a table, a read clause, an empty
> command mixin, a stub sweep, and a Discord command that calls a function that does not exist.
> `merge_slots` is a table and a dataclass. `src/sessions/tmux.py` is not a file. Sessions,
> worktrees, and the blocked-state projection all ship behind flags that default to off — the
> projection is computed, compared to the legacy scan, logged when it disagrees, and then
> ignored, because the legacy scan still decides. You have built a very careful skeleton of a
> system and switched it off.
>
> Meanwhile one person shipped thirty-nine working tools in nine months by refusing to build a
> coherent system. He built thirty-nine incoherent ones that compose through files and CLIs, and
> by throughput he is beating you badly. The transactional coherence you are proud of is
> precisely the property that made your velocity slower than his. And every differentiator you
> name — unattended operation, crash survival, queryable history — is *conditional on finishing*,
> which is the exact thing coherence makes hard.
>
> You are building a platform for a fleet you do not have, using a fleet you do not have.

That is a good argument. My honest answer, in three parts:

**It is right about the diagnosis and wrong about the remedy.** The correct comparison is not
"one coherent system versus thirty-nine tools" — it is *one thing to keep working versus
seventeen*. The flywheel's composition cost is paid by the operator, continuously, forever, and
it is invisible in a feature list: seventeen binaries, several of which do not run on Windows at
all, every one of them bus-factor 1, every one of them under a stated no-outside-contributions
policy, several averaging 20-77 commits/day with corresponding API churn. The throughput
comparison flatters the flywheel because it counts *shipping* and never counts *owning*.

**The velocity criticism is correct but misattributed — and the flags point is a fair hit I have
to concede.** Memory and playbooks were not paused by schema coupling; the todo says plainly they
were "half-baked and under-tested," and their value was conditional on a core that runs. Pausing
without deleting was the right call and is evidence this project can cut scope, which platform
projects usually cannot. But the dark-flags observation lands. Default-off is the correct way to
merge a subsystem; it is *not* a correct place to leave one, and three of them plus three
schema-only subsystems is not a rollout plan, it is an inventory. The velocity problem is real
and its proximate cause is upstream of architecture: the daemon cannot run a real session on the
development machine, so nothing can be switched on with confidence, so everything stays off.
That is a single blocking dependency wearing six costumes.

**The strongest form of the argument deserves a concession, so here it is.** agent-queue is
probably not worth building *as a product*. It is plausibly worth building as *this operator's
system*, and only if the readiness question in §1 turns out to be a question you actually have.
So make that falsifiable rather than arguing about it:

> **Kill criterion.** Ninety days after C1 with real workloads: if the median number of
> simultaneously READY-and-unblocked tasks across all projects is under four, the multi-agent
> premise is unused, `explain` is answering a question nobody asks, and the daemon is not earning
> its complexity. At that point the right move is to retire it to a small tmux launcher plus the
> `sessions` table and the exit classifier — which are the two pieces that would still be
> earning their keep.

Write that number down now, before the data exists, so it cannot be renegotiated later.

---

## Appendix — verification log

Claims checked directly against source rather than taken from the reports:

| Claim | Result | Evidence |
|---|---|---|
| `token_ledger` has `model`/`input_tokens`/`output_tokens`, unwritten | Confirmed — and `record_token_usage` already accepts all three as kwargs; the live writer just never passes them | `src/database/tables.py:252-267`; `src/database/queries/token_queries.py:17,38`; `src/orchestrator/execution.py:911` |
| Both runtimes already compute the input/output split and discard it | Confirmed | `src/runtimes/claude_sdk.py:655-658`; `src/runtimes/acpx.py:240-253` |
| `AgentOutput` has no place to carry model or split | Confirmed | `src/models.py:850-867` |
| Scheduler caps enforced against a snapshot, outside the write | Confirmed — safe today (single writer), not an invariant | `src/scheduler.py:344-390`; `src/orchestrator/core.py:2180-2301` |
| `is_blocked` recompute is genuinely same-transaction | **Confirmed, and better than assumed** — `recompute_blocked(seed, *, conn)` requires the caller's connection and never opens its own; 9 call sites all inside `engine.begin()`. Only post-commit work is audit event emission | `src/database/queries/blocked_state.py:220,357`; `task_queries.py:197,249,264,297`; `dependency_queries.py:44,349,368` |
| …but the projection is not authoritative | `work_graph.blocked_state_authoritative` defaults `False`; legacy scan still decides, divergence only logged | `src/config.py:1158`; `src/orchestrator/monitoring.py:85-98` |
| Typed edges used in the readiness predicate | Confirmed — 8 CHECK-constrained values, `dep_type` in the composite PK, one predicate clause per blocking kind | `src/database/tables.py:118-145`; `src/models.py:184`; `blocked_state.py:77-202` |
| **Gates are schema-only** | `gates`/`task_gates` exist and the blocked predicate reads them; `_sweep_gates` is a stub, `GateCommandsMixin` is an empty class, no create/resolve/list command exists. Discord registers `/gates` → a command that does not exist | `tables.py:176-217`; `core.py:2093-2113`; `src/commands/gate_commands.py`; `src/discord/slash_commands.py:350-362` |
| **Merge slot is schema + dataclass only** | No acquire/release/lease-break anywhere; the only mention of `break_expired_merge_slots` is a stub's docstring | `tables.py:382-394`; `src/models.py:535-551`; `core.py:2115-2124` |
| **`src/sessions/tmux.py` does not exist** | Registry imports it in a `try/except ImportError` that always fails; config default moved off tmux | `src/sessions/__init__.py:103`; `src/config.py:841-849` |
| Rest of `src/sessions/` is built | `SessionProvider` ABC + `Cap` gating, `FakeProvider`, `SubprocessProvider`, `SessionReconciler`, `classify_exit`, drain-ack command + CLI, full `_cmd_task_close` | `src/sessions/{provider,fake,subprocess,reconciler,exit_classifier}.py`; `src/commands/session_commands.py:292,323` |
| Worktree-per-task is implemented but off | `WorktreeSlotManager` is real code (slots, sentinels, salvage, setup hash); `worktrees.enabled` defaults `False`, so every git kind falls back to `exclusive-clone` | `src/orchestrator/worktree_manager.py:95`; `src/orchestrator/workspace.py:55-62`; `src/config.py:921` |
| Multi-kind acquisition is all-or-nothing | Confirmed — but by **compensating rollback**, not one transaction; canonical `(kind_id, position)` sort is real | `src/orchestrator/workspace_attachments.py:39,98` |
| `provider_cooldown` is a real scheduler filter | Confirmed — excludes cooled agents before assignment. **State is an in-memory dict; lost on restart** | `src/scheduler.py:147,247`; `src/orchestrator/core.py:270`; `execution.py:1508` |
| No dangerous-command classifier exists | Confirmed — every `rm -rf` / `dangerous` / `destructive` hit is a comment, a docstring, or a static command-name exclusion list | `src/cli/auto_commands.py:67`; `src/mcp_registration.py:48-55` |
| `env_scrub` and `doctor` are built | `scrub_env` with 3 live callers; 11 registered doctor checks, 4 reserved-but-unregistered (`sessions.stale`, `tmux.server`, `worktrees.orphans`, `leases.stale`) | `src/env_scrub.py`; `src/doctor/builtin.py:676-690`; `src/doctor/models.py:120-127` |
| Counts | **32** tables, **28** Alembic revisions, **228** test files, `.github/workflows/tests.yml` exists (`alembic upgrade head` + `pytest -n auto`) | — |
| `is_blocked` is graph-only; capacity reasons deliberately not persisted | Confirmed in spec | `docs/specs/design/work-graph.md` §4.1 |
| Parallel plans execute serially via the shared parent branch | Confirmed in spec, accepted with reasons | `docs/specs/design/worktree-execution.md` §4.4 |

**Two pieces of dead or near-dead code worth a ticket, found in passing:**
`recompute_blocked_waves` (`blocked_state.py:281`) has zero production call sites — bulk graph
creation does not use it. And `recompute_all_blocked` / `evaluate_blocked` (`:432`, `:416`) were
written as the `aq doctor` repair path, but no blocked-state drift check is registered in
`src/doctor/builtin.py`.
