---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:b26948267049ce66b380c6e83039d749e42c9800a97769295d9e9ffbbfb743f7
source_sha256: sha256:1d9d609da7bb5c2fa6f6ac8e80a281a508accebe2f14ceb4b9eedf032fa7b8ee
contract_fingerprint: sha256:422c596ade1f08822936de82ae7ba1a574cb59f081ff9fa9bd9f4e91d5f2d132
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
