---
playbook_id: promotion-continuous
artifact_sha256: sha256:bbe82bef84f8179678ff55032136a5432abe24eeaeae9042a56b5cefc30ad969
source_sha256: sha256:1969f871c2f67f6913dc2f16b27ce225a24caa3a013a75ed03664b33103ff445
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


Typed-source port: the six existing commands are now explicitly declared in
`required_capabilities`, with exact binding paths, recipients and terminal
outcomes stated in the source. This enables strict source-policy alignment.
Executable rules, steps, command contracts and feature requirements are unchanged;
only source provenance, source identity, compilation time and capability metadata
change. `message_send` remains once per durable event; namespace suppression,
collection scans, diagnostics and scripts would add behavior this policy does
not have. No state schema version is introduced.

The baseline is the shipped graph at 52d2efe4cb5380e6c1889b80f2bf2cce04af9473,
source sha256:92457830a080559adc64d10202394da69ff316921263c53421b786262fdb0744.
The observed installed source was older,
sha256:7cb8531f574f8242dd7cb42a9649b5a1ab004fec74f072bb76e28d53553223f3;
it lacked the migration-refusal paragraph. This port retains the shipped repair.
`tests/test_promotion_continuous_equivalence.py` compares the complete executable
graph and runs 42 recorded scenarios against both artifacts through the V2 engine.
The frozen baseline and scenario expectations live under
`tests/fixtures/playbooks/promotion-continuous-equivalence/`.
This is repository evidence only; no installation or activation was performed.
