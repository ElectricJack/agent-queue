---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:8bd9291262107650b1dc4ef22fe2c2c43a7fccf1bf3ef78b9e3c9d7b5bb4b3e0
source_sha256: sha256:983341e1847a0b00b4fb4402aee847a5b56e84cca23876ed2a4417ef67ccd183
contract_fingerprint: sha256:fe7d28213ac0bf62c105621980a6ff1a83348f8f5424d079acb9bb09ee217cc0
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
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
