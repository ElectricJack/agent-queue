---
playbook_id: hierarchical-delivery
artifact_sha256: sha256:b78b12e31efc1cc5338c00a13541dd2657abc99a3c08350e4e73ee30b506fdcb
source_sha256: sha256:7046de82db414a94cb300ae6f2920e9e7f8501dca8394e6ca670b467ad33116c
contract_fingerprint: sha256:1b9a053e0dfb874435d0b813bbd4771990287bef91525cecb38e5c5f98102770
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
deterministic command. Failed-child `block` remains a failed terminal with the
parent suspended; `ask` opens one ordinary deduplicated human gate. Repair close
and resolution-push facts are explicitly lifecycle-only and are not success proof.

No activation is created by this bundle. Task 11 owns operator activation.
