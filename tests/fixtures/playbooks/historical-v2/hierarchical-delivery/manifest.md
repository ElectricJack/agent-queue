---
playbook_id: hierarchical-delivery
artifact_sha256: sha256:579b8a1a92b66d885acba6417483e5426f2bab6691c55bd8810ee3bd087a4d82
source_sha256: sha256:adfdc677da623f46f75d3f1acb3b0485430a4f0462ad2ef45d4aa42e8dee6ddc
contract_fingerprint: sha256:67ab605057f2910bdf735c193f0bc4954af8c02629b7a4c37f3b827c98743d3f
questions_resolved: []
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
  plugin_tools: []
profiles_referenced: []
---

# Review decision

Approved as a disabled offline-reviewed artifact. Every route uses a registered
deterministic command. Failed-child `block` ends the run as blocked with the
parent suspended; `ask` opens one ordinary deduplicated human gate. Repair close
and resolution-push facts are explicitly lifecycle-only and are not success proof.

No activation is created by this bundle. Task 11 owns operator activation.
