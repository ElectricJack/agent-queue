---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:f9933f7a1fb84c0e310dde61bf3f8d52ef8a315f81e341e5594aba0a904c49c9
source_sha256: sha256:ec1658aceabaf2e5dc42957f5a53b5a7e73b06dd9ec648496b3b39fd73ce8323
contract_fingerprint: sha256:95a39ec316ba3a8d9e90b8b75ce47310984c4b5255f16b400a3bc44265e68154
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
  - ci_repair_adopt
  - ensure_task
  - escalation_create
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `ci-main-sentinel`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
