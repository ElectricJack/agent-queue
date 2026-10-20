---
playbook_id: provider-failover
artifact_sha256: sha256:b130c149b5cd6a89dc23c0fe1610f131699fe31e1c1137095c0fd111918452c1
source_sha256: sha256:d62f4ab216823ede9c2e6471f06415d226f8fa5038fe281242d6218b0ba76769
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
