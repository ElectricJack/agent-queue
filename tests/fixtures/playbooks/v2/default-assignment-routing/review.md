---
playbook_id: default-assignment-routing
artifact_sha256: null
source_sha256: "sha256:88966b7b509db302d5b0707ea38b106f3fc8950e264155e4be1024f92f65f1b2"
contract_fingerprint: null
reviewed_by: "aq task solid-harbor.52 (worker-standard-high-claude); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: rejected
questions_resolved: 2
blocked_on:
  - "unknown_event: assignment.route.requested is not registered in src/event_schemas.py"
  - "unknown_profile: src/profiles/defaults/ ships no assignment-routing profile"
capabilities_granted:
  aq_commands: []
  harness_tools: []
  plugin_tools: []
profiles_referenced: [assignment-routing]
---

# Reviewed V2 proposal — `default-assignment-routing` (not approved)

`decision: rejected` is the locked vocabulary's value for "recorded, and no
activation may reference it" (child plan §3.4). It does **not** mean a human
read this playbook and disliked it. It means the proposal cannot be made
activatable without a change that belongs to another package, and recording it
as approved would be an authority claim this review cannot support.

There is no `artifact.json` in this directory, deliberately: the compile
produced no activatable artifact, and shipping one would let a later
`test_every_command_resolves` pass over a definition nothing may run.
`diagnostics.json` is the recorded compiler output.

## Compiler questions and decisions

**Q1 — `assignment.route.requested` is not a registered event type.**
`src/orchestrator/assignment_routing.py:458` builds and dispatches this event
for real on every routing batch, but it is absent from `src/event_schemas.py`
(140 registered types, none matching), so `validate_definition` raises
`unknown_event` and refuses the rule's trigger. This is a genuine spec
divergence in the live tree, not a compiler defect. **Decision: do not register
it from Package 6.** The event registry is Package 1's surface, and adding a
schema would newly subject a live dispatch path to schema validation — a
behaviour change that needs its own test and its own review, not a side effect
of a fixture commit. Recorded here so the cutover report shows it.

**Q2 — `assignment-routing` is not a known profile.** `lower_assignment` takes
the step's `profile_id` from the source's `role: assignment-routing`
frontmatter, and V2 requires every `llm` step to name a resolvable profile.
`src/profiles/defaults/` ships ten profiles and none of them is
`assignment-routing`; under V1 the `role` key was a label and the model came
from `llm_config.intelligence_class: fast-low` instead, so nothing ever needed
the profile to exist. **Decision: do not invent one from Package 6.** The
shipped profile set is Package 0's and the operator's; a profile authored here
would be a capability grant written by a fixture, which is exactly what child
plan §4.1 forbids.

## Semantic diff versus the V1 graph

Not applicable in the usual sense: this playbook never had an embedded action
graph. `src/playbooks/pipeline_lowering.lower_assignment` derives the whole
proposal from the frontmatter and prose — one `llm` step plus its terminal, with
`max_tokens: 4096` becoming both the output and total token budget and a
300-second timeout. The proposal's shape is fully determined by that function
and is reproducible with
`python scripts/rebuild-reviewed-playbook-artifacts.py default-assignment-routing`.

## Capabilities and why each is needed

None granted, because nothing is approved. For the record, the proposal needs no
AQ command, no harness tool and no plugin tool: its single step is an `llm` step
whose output is the routing decision, and the caller
(`AssignmentRouting.routes_for`) applies it.

## AI profiles, budgets, and output schemas

The unresolved profile is Q2. The budget the lowering derives is
`max_calls: 1`, `max_output_tokens: 4096`, `max_total_tokens: 4096`,
`timeout_seconds: 300`, taken from the source's `max_tokens`. The output schema
is `{"type": "object", "additionalProperties": true}` — permissive, because V1
parsed the model's JSON without a schema and narrowing it here would be a
behaviour change smuggled in as a compile.

## Accepted behaviour differences

None accepted, because nothing is activated. Both blockers must be resolved and
this fixture rebuilt and re-reviewed before `default-assignment-routing` can
appear in an activation set.
