---
playbook_id: parent-integration
artifact_sha256: sha256:8522e27ac2199f3d77efe0c8305d390f4abe668b03b5ded50e429ccafdc5d7ed
source_sha256: sha256:f0719f9ea0f5ddd367f9130503816e428905f123ea440b101b58b9890cf8fd93
contract_fingerprint: sha256:3e4cad60a27bfb8064b6897a85b865fe667f93fd9710ca9242c444b3b47322fb
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - delivery_promote
  - gate_create
  - integration_checkpoint_parent
  - integration_complete_parent
  - integration_delivery_readiness
  - integration_file_children
  - integration_parent_verify
  - integration_reconcile_promotion
  - integration_record_repair
  - integration_repair_dispatch
  - integration_repair_start
  - integration_repair_timeout
  - integration_transfer_owner
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

Integration CI repair refreshes the reconciliation contract and explicitly maps
`continued` and `superseded` to completion, and `waiting` and `target_moved` to
failure, matching the server's current outcome classifications. This preserves
the exact remote reconciliation requirement and grants no additional capability.
The new digest still requires operator review before activation.

# Review decision

System-scoped shared parent route for any project's train policy. The graph is
the reviewed `agent-queue-parent-integration` graph unchanged: the same rules,
steps, transitions and command contracts, compiled at system scope, with each
source reference moved to the same rule heading of the shared source. A system
activation serves only projects whose frozen policy route names this playbook
(`INTEGRATION_LIFECYCLE_PLAYBOOK_IDS`, `src/playbooks/services.py`), never every
project's lifecycle events. Commands receive durable subject identities; CI
trust, Git refs, repair budgets, and promotion authority stay server-owned.
Import and activation require operator review. The bundle has no automatic
activation.

CI repair `clear-quest-21` refreshed the record and timeout command fingerprints
for the new `supervisor_recovery` action. Reviewed rules, steps, transitions,
capability grants and source text are unchanged; no-progress incidents remain
owned by the command service.

Phase 2 adds an inactive parent decision table over the existing primitives.
It selects receipt-driven collection and intent read-back, the existing failed
verification recovery, shared verifier filing/lease and exact CI verification.
Failed children, conflicts, old repair dossiers and expired writer budgets use
explicit no-default gates. Import remains write-if-absent and no activation or
engine transfer is authorized by this bundle. Operator rollout review binds the
new digest before cutover.
