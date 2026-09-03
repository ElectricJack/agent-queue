---
playbook_id: default-pipeline
artifact_sha256: "sha256:fd28a4ca3a4f0be27fd8253e132699bd5757461e2b07c78d842dfecfc2fd4cea"
source_sha256: "sha256:4c5af240e58db3c4a3ce6012dd933305965054a6afeb59827952efd1ecdab123"
contract_fingerprint: "sha256:64868157d0d987401d13d954e0bd3edc0c01fc427c626b2947d760a57cc855fe"
reviewed_by: "aq task solid-harbor.52, re-recorded by nimble-apex-17 (worker-standard-high-claude); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: approved
questions_resolved: 3
capabilities_granted:
  aq_commands: [add_dependency, ensure_task, gate_create, get_downstream_tasks, task_batch_commit]
  harness_tools: []
  plugin_tools: []
profiles_referenced: [final-reviewer, reviewer, spec-ingest]
---

# Reviewed V2 artifact — `default-pipeline`

## Compiler questions and decisions

**Q1 — where does the semantic body come from, now that the prose carries no
graph?** The V1 source's embedded action graph was frozen byte-for-byte at
`tests/fixtures/playbooks/v1/default-pipeline.md` before the rewrite, and this
artifact is `src/playbooks/pipeline_lowering.lower_pipeline` applied to *that*
file. **Decision: lower the frozen V1 graph rather than re-derive a body from
prose.** An LLM re-derivation would be an unverifiable claim of equivalence;
lowering the graph the fleet actually ran makes equivalence true by
construction, and any future divergence shows up as a semantic diff against
this recording. The prose rewrite is then held to the artifact by
`tests/test_shipped_playbook_sources.py::test_shipped_sources_declare_every_identifier`,
which fails if the prose stops granting a name the artifact emits.

**Q2 — the lowered source references point into a file that no longer contains a
graph.** Every `source` in the lowered body named a line inside the old JSON
fence (lines 207–293 of a 298-line file); the rewritten source is 204 lines and
those references are out of range, which `propose()` reports as
`source_ref_out_of_range`. **Decision: remap each reference onto the prose that
authorises it** — a rule onto its `## Rule: <id>` heading, a step onto the
numbered prose item that describes it, a terminal step onto
`## Failure handling, uniformly`. The mapping is `PIPELINE_STEP_PROSE` in
`scripts/rebuild-reviewed-playbook-artifacts.py` and is part of what a reviewer
checks. Source references are presentation-only (spec §4.8) and do not enter the
executable fingerprint, so this changes what a reviewer *sees*, never what runs.

**Q3 — five argument names and four bindings the prose did not previously
mention.** `propose()` with `enforce_inventory=True` refused
`get_downstream_tasks`, `gate_create`, `add_dependency`, `project_id`,
`dedup_key`, `title`, `description`, `task_id`, `depends_on`, `dep_type`,
`waiter_task_ids`, `question`, `proposal_id`, `spec_path` and the bindings
`review`, `final`, `downstream`, `dep`, because none appeared as a backticked
identifier in the old prose — they only existed inside the JSON. **Decision: add
each one to the prose as a backticked identifier in the sentence that describes
what it does**, rather than relaxing `enforce_inventory`. That is the whole
point of the inventory rule (spec, "Metadata ownership"): the source, not the
compiler, grants an executable name.

## Semantic diff versus the V1 graph

**Empty on every executable field.** The artifact is the deterministic lowering
of the frozen V1 graph, so rule ids, triggers, guards, entry steps, command
names, argument expressions, `for_each` sources and bindings, transition
targets, and terminal outcomes are the V1 values unchanged. What a reviewer
should confirm by reading:

- five rules, ids unchanged: `per-task-review`, `per-branch-final-review`,
  `spec-ingest-on-approve`, `proposal-ready-gate`, `commit-on-gate-resolve`.
  Neither superseded id (`task-created-routing`, `worker-filed-triage`, the two
  `src/playbooks/routing.py` suppresses) appears;
- 19 steps, of which two are `foreach` loops
  (`per-task-review--gate-downstream`,
  `per-branch-final-review--gate-downstream-pr-merged`), each with a body step
  that re-enters its loop;
- the `review:task:{event.task_id}` dedup key template renders to exactly what
  `src/doctor/integration_checks._review_dedup_key` builds, which is what keeps
  the `integration.unreviewed_prs` alarm armed across this rewrite;
- `per-branch-final-review--link-blocks` routes **both** success and failure to
  `fetch-downstream-branch`. It is the one step in the playbook whose failure
  does not end its rule, and it was that way in V1.

The only fields that differ from a naive V1 read are presentation: `source`
references (Q2) and the step id namespacing V2 requires (`<rule-id>--<node-id>`,
plus `-body` for a loop body), which V1 expressed as per-rule `nodes` maps.

## Capabilities and why each is needed

Five AQ commands, one per distinct thing the pipeline does. No harness tools and
no plugin tools: every step is a `command` step, so nothing in this artifact
reaches a model or a shell.

| Command | Why the pipeline needs it |
|---|---|
| `ensure_task` | creates the reviewer, final-reviewer and spec-ingest tasks, dedup-keyed so a repeated event converges rather than fanning out |
| `add_dependency` | records `discovered-from` (review ← reviewed task) and `blocks` (final review ← per-task review) edges |
| `get_downstream_tasks` | enumerates the dependents that must be gated |
| `gate_create` | raises the `task`, `pr-merged` and `human` gates |
| `task_batch_commit` | writes an approved proposal into the task graph |

`capabilities_granted` above is a **ceiling for comparison only**. Nothing under
`src/` reads this file; production capability comes from the profile
(`CapabilityPolicy`) and the database activation, both server-owned. The audit
in `tests/test_default_playbook_v2_artifacts.py` fails when the artifact needs a
capability this list omits, and never the other way round (child plan §4.1).

## AI profiles, budgets, and output schemas

**None.** This artifact contains no `llm` step, therefore no profile, no token
budget, and no output schema to review. The three profile ids in
`profiles_referenced` are *argument values* to `ensure_task` — they name the
profile the created task will run under, not a profile this playbook executes
as — and all three ship in `src/profiles/defaults/`.

They are nonetheless **dependencies of this artifact**, and
`compiled_against.profiles` records a capability fingerprint for each. That is
the re-record `nimble-apex-17` made: the first recording left the map empty,
because the compiler only snapshotted a profile a step runs *as*, so widening
`reviewer` — the profile this playbook hands review work to — could never stale
the approved artifact. What was reviewed here is not just "the three ids exist"
but *what those three profiles were allowed to do* on the day of approval, so a
capability change to any of them now shows up as `stale_contract` in activation
health and as drift in `aq playbook release-check`. Nothing else in the artifact
moved: `compiled_at`, `source_hash`, every rule and every step are byte-identical
to the first recording, and `contract_fingerprint` is unchanged because it covers
`compiled_against.commands` alone.

Deliberately, the `ensure_task` steps pin `profile_id` but no
`intelligence_class`, so the assignment-routing playbook still chooses the class
for the tasks they create. That is unchanged from V1 and is documented in the
authoring source.

## Accepted behaviour differences

1. **Step identity is namespaced.** V1 node ids were unique per rule; V2 step
   ids are unique per artifact, so every id gained a `<rule-id>--` prefix and
   each loop gained a `-body` step. Run history keyed on V1 node ids will not
   join to V2 step ids. Accepted: the ids are not part of any external contract.
2. **Loop bodies are explicit steps.** V1's `for_each` was a property of a node;
   V2 lowers it to a `foreach` step plus a body step that re-enters it. The
   observable effect — one `gate_create` per downstream dependent, failures not
   aborting the rule — is identical.
3. **Source references point at prose, not at a graph** (Q2). Presentation only.
4. **The V1 compiler can no longer compile the shipped source.** This is the
   intended consequence of the rewrite, not a regression in this artifact.
   `src/vault.py::ensure_default_playbooks` never overwrites an existing vault
   copy, so no running fleet loses its V1 pipeline; a *fresh* install between
   Package 6 and Package 7's cutover has a default pipeline that neither runtime
   executes until an operator activates this artifact. That window is Package
   7's to close and is recorded here so it is not discovered later.
