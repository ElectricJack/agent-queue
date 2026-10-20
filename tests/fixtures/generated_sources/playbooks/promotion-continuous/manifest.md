---
playbook_id: promotion-continuous
artifact_sha256: sha256:afa967d75ab60cd197d7fd9023b8350c738285eff4c84da60de19d12fc957cba
source_sha256: sha256:92457830a080559adc64d10202394da69ff316921263c53421b786262fdb0744
contract_fingerprint: sha256:0fe70d5e2def4bd1e7282c1713a1a5971a64309b5f8ebfd40b11536e3670d3a8
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

Migration-admission repair re-review: the semantic diff adds all three new
registered refusals (`migrations_not_additive`, `migration_range_invalid`, and
`breaking_migration_override_forbidden`) to each affected command's existing
operator-notification path. Success edges, event bindings, command inputs and
publication gates are unchanged. No step supplies `allow_breaking_migration`;
notification refusal still terminates as failed without a retry. The refreshed
contract fingerprints include `integration_backmerge_source`. Engine regression
coverage exercises request, hotfix, backmerge and continuous-source refusals.
This records the repository review only; neither bundle is activated by this repair.
