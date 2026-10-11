---
playbook_id: promotion-continuous
artifact_sha256: sha256:11c495a716342cfb783a9fdd24c4b6179f6234996dd4e119511872be8fed86ad
source_sha256: sha256:7cb8531f574f8242dd7cb42a9649b5a1ab004fec74f072bb76e28d53553223f3
contract_fingerprint: sha256:00fc53c499a1e1c3e24db8eb7a4186463a3ce984b381b75671d2724dd3fe7cc6
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - integration_promotion_policy_input
  - promote_request
  - promote_cancel
  - integration_promotion_publish
  - integration_backmerge_source
  - message_send
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Review decision

The graph follows the source decision rules, retains daemon-only publication,
and binds only a stored project step to an enabled reviewed activation. It ships
disabled. Every source and command fingerprint is rebuilt from the reviewed
prose and registered contracts. No unresolved compiler questions remain.
