---
playbook_id: promotion-request
artifact_sha256: sha256:389999dfca3102fd3df88169fff23c2f872deb35ddd790a1589639bc25aae81a
source_sha256: sha256:10c345b8f224d49ea31cb1f306bd54b619a5ef255c6360950664ed33c850be56
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

## Reusable building-block port

The port declares the existing six capabilities explicitly and states typed
result paths, ordered decisions, exact notification inputs and outcome handling
in the source. This enables the compiler's stronger source-policy alignment
checks. Executable rules, steps, command pins and capability inventory remain
unchanged apart from source locations. No new runtime feature is required.

Keep per-event `message_send` and server-owned promotion/backmerge commands.
State notifications would suppress separate events and change sender/provenance;
there is no collection, observation scan, script or agent step to replace.
State version and scripts are therefore deliberately undeclared.

The immutable equivalence baseline is
`tests/fixtures/playbooks/scenarios/promotion-request/shipped-before.artifact.json`
(artifact `sha256:e23b9ec529926ed9c6207b9f5843d77942bb9690bc9694d8c4b19938f1445a85`,
source `sha256:1fe223117055fe42e0e70be6a00ea4827c834b21e886f461c7c9e4b62dc53d26`).
`events.json` and `tests/test_promotion_request_playbook.py` compare old/new
engine traces, exact command inputs, all registered outcomes, migration refusals,
duplicate delivery and separate notification events. Existing promotion-policy
tests remain unchanged.

Supervisor decision on task `agile-current-15`: use the newer shipped policy,
not the installed October 7 bundle. Installed source digest
`sha256:0ec9297ef1331f68b116fdc1534a4fea1d0c5570bba8aa085669f011805f78a8`
and artifact `sha256:46e53d4bd804e4a2e68c19898970356837e2df48b97662ea867950570b22f8a7`
lag the shipped migration-refusal repair `149b20afc`. Deliberately installing
this port also installs that repair. This source/artifact is for supervisor
review; no installation, activation or change to the installed copy is performed.
