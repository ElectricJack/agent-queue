---
playbook_id: provider-failover
artifact_sha256: sha256:c9c654570850380d58ad9064aa0d12def42092857aba848aa572e209b4d6b2d4
source_sha256: sha256:099fb99f31755c56015d3cd69feab0e98b75747241c3e7a14779dadae204f1a4
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
