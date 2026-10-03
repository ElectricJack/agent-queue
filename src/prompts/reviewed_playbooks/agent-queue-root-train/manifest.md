---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:387536e8e7b0a903270073ae6ce69f51a4dd4eb44ddd3cfdf50939b84d8ae6db
source_sha256: sha256:fc4003fbd27f7e090bb9ea294663ded27032d960e636813e33f7b063caec6001
contract_fingerprint: sha256:1b70a6bcef122a31aa89b507432f8cd94b7e750b698ad6538e6669e4384e6bbd
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - integration_build_candidate
  - integration_ci_evidence
  - integration_repair_close_current
  - integration_cleanup
  - integration_promote_main
  - integration_release
  - integration_repair_dispatch
  - integration_seal
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Review decision

Project-scoped candidate for agent-queue train configuration. Adapted from the
historical integration graph with a new identity and current command contracts.
The parent route handles `already_completed` as idempotent success and `failed`
as failure. Commands receive durable subject identities; CI trust, Git refs,
repair budgets, and promotion authority stay server-owned. Import and activation
require operator review. The bundle has no automatic activation.

## Subject table handoff

The disabled bundle includes a source-owned integration decision table.
Existing event rules remain available for feature-off rollback. Each subject
retains its exact artifact pin. Gate choices and replay deadlines are explicit;
root publication needs the observed publisher fence, distinct from repair
writer authority. Agent Queue retains authorized continuation; the generic
template retains a no-default exhaustion gate. Import/activation and production
cutover evidence remain operator-owned.
