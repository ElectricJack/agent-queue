---
playbook_id: parent-integration
artifact_sha256: sha256:65f970021fdf5c73b8aeabdd4bbbf22f2318d3c198d1176731b7196de1a3bf51
source_sha256: sha256:aad657532402ee10ae5d02acf4b1ebe0eda28f3a3914deca81fd62767789f81b
contract_fingerprint: sha256:785231157149d5982aca42894c2c0fb73653624f9b1d65303afb9fbfbb73d50e
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
