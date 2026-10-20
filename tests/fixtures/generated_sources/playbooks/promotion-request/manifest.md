---
playbook_id: promotion-request
artifact_sha256: sha256:8d2d2bfabd668543c500c291c801a293d7c4d9bc1a38088b657cfb0e26091607
source_sha256: sha256:1fe223117055fe42e0e70be6a00ea4827c834b21e886f461c7c9e4b62dc53d26
contract_fingerprint: sha256:cda051fbaf920279cbbe2655e5651b41f4a1f7d53709328002fea78ecb88333e
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
