---
playbook_id: default-assignment-routing
artifact_sha256: "sha256:14324858444c042d58d2da74211b4ca6a826419b3398ef9a0d521103f432bbbc"
source_sha256: "sha256:9f863ef0d43229c745c31c0fabd6b49baf81a1c389db83ed3ebc1bfb451fd955"
contract_fingerprint: "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
reviewed_by: "aq task agile-impact-36 (worker-deep-high-codex); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: approved
questions_resolved: 2
capabilities_granted:
  aq_commands: []
  harness_tools: []
  plugin_tools: []
profiles_referenced: [playbook-compiler]
---

# Reviewed V2 artifact — `default-assignment-routing`

## Compiler questions and decisions

**Q1 — register `assignment.route.requested` or leave the live emitter outside
the event contract?** Decision: register the exact payload already emitted by
`AssignmentRoutingCoordinator`: `project_id`, `tasks`, `options`,
`options_hash`, and `catalog_hash`. This changes no payload and lets V2
type-check the trigger.

**Q2 — which existing profile represents the V1 `fast-low` direct call?**
Decision: use `playbook-compiler`. Its shipped `default_class` is `fast-low`,
and this step publishes no tools. The V1 `role: assignment-routing`
discriminator remains unchanged; `profile_id` is V2's AI identity.

## Semantic diff versus the V1 graph

This playbook never had an embedded graph. `lower_assignment` preserves its V1
shape as one `llm` step and one terminal. `max_tokens: 4096` becomes both the
output and total-token limit, with the existing 300-second timeout. The body is
reproducible with `python scripts/rebuild-reviewed-playbook-artifacts.py
default-assignment-routing`.

## Capabilities and why each is needed

None. The LLM returns routing data to `AssignmentRoutingCoordinator`; the
artifact publishes no AQ, harness, or plugin tools.

## AI profiles, budgets, and output schemas

`playbook-compiler` resolves the same `fast-low` class used by V1. The output
schema remains permissive because V1 parsed a JSON routing object without a
narrower schema; narrowing it during migration would change behavior.

## Accepted behaviour differences

1. V2 validates the routing payload against the emitter's now-registered schema.
2. V2 names `playbook-compiler` while V1 carried `fast-low` directly. Tool
   publication remains disabled, and both resolve to the same class.
