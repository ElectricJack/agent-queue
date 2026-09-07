---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:73fbad04387fe9f90291b5ac5425c12d869dc99ee5d5a28cf805fb35762c5700
source_sha256: sha256:45d286d59b7500db112d54d529b330ddef3cfdd369c98b386fe789dcb55aa7e9
contract_fingerprint: sha256:787834ee710a0d62d5159fd6723f67f7faf6843af65dbd561a1e31e4c75f7c5f
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
  - ensure_task
  - gate_create
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `ci-main-sentinel`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
