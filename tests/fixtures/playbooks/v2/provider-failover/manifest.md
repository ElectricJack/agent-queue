---
playbook_id: provider-failover
artifact_sha256: sha256:d2281cb25e810572814bf255db717933ad5006b1c3dc7cfb7188cf782273f5b8
source_sha256: sha256:c86cb847dacca5a86596fe88feabcfd8695d2fdf066a172cc4955250cda2f983
contract_fingerprint: sha256:d412e6351df6d6e6e56ccce2023b408e6865fe1a1f5d771747d0678468fae622
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - provider_availability_notify
  - provider_reroute
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `provider-failover`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
