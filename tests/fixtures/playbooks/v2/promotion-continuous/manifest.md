---
playbook_id: promotion-continuous
artifact_sha256: sha256:b272026e9debbbba6d1d920b59f0fe3b3c819a4b6997e70db7133e5434ab51a9
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
