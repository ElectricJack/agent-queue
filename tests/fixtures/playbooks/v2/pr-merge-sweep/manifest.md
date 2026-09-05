---
playbook_id: pr-merge-sweep
artifact_sha256: sha256:90d121194684e05c91e2b00dc2273b103b07dab800f78a8ffee98c3788ac33e1
source_sha256: sha256:fa8a07f2fad26a7386b7ce415feb51d29f550b983ea467955c1fc718206690eb
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
