---
playbook_id: promotion-request
artifact_sha256: sha256:395d6f8f50af9bdd9bac5543bf4f1d0de824aeaf531c380139abce3dcc9cdbde
source_sha256: sha256:0ec9297ef1331f68b116fdc1534a4fea1d0c5570bba8aa085669f011805f78a8
contract_fingerprint: sha256:d05b6693ec8f2dc946684dc1c442464b53e899fc39a0076555ce329217436e19
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
