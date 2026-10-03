---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:4ca7856e20e09aeef0faa2534a664fdd3bf5883653a68799ce2da6ef55c26f94
source_sha256: sha256:7681a298cbbd8da569dd3099c2d0a8a12ae424c2eacd751109c162ca5f6b0921
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

# Review decision

Project-scoped candidate for agent-queue train configuration. Adapted from the
historical integration graph with a new identity and current command contracts.
The parent route handles `already_completed` as idempotent success and `failed`
as failure. Commands receive durable subject identities; CI trust, Git refs,
repair budgets, and promotion authority stay server-owned. Import and activation
require operator review. The bundle has no automatic activation.

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

CI repair `fleet-dune-24` reconciles the current promotion contract explicitly:
`applied`, `superseded` and `continued` complete the event rule; `not_applied`,
`waiting`, `target_moved` and `invariant_error` fail. Supersession or continuation
does not create a delivery receipt, and waiting cannot report reconciliation
success. All other graph semantics and grants are unchanged. Fingerprints bind
the current implementation; operator import and activation review remain required.
