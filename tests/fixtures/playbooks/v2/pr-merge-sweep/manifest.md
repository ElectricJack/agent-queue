---
playbook_id: pr-merge-sweep
artifact_sha256: sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525
source_sha256: sha256:38c3f724c0f68fa7039118393c97d3ed11c72bcb58f0fc045bb7a4284600b8d7
contract_fingerprint: sha256:7d1dab70ae2d72185eace7dedc3836b1bdfeed3ce168936036372ee8e059aaf7
questions_resolved: 2
capabilities_granted:
  aq_commands:
  - ensure_task
  - task_route
  harness_tools: []
  plugin_tools: []
profiles_referenced:
- pr-merger
---

# Playbooks V2 artifact manifest — `pr-merge-sweep`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
