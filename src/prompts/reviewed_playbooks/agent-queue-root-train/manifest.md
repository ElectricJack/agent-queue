---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:0c25f12e5f16bdf28a4a320123572ade237d598899ab8571ae1c023eaf94370f
source_sha256: sha256:fc4003fbd27f7e090bb9ea294663ded27032d960e636813e33f7b063caec6001
contract_fingerprint: sha256:33581303bf8556083f103154d1b94b810df3c398a49c2dbe90bcf68d37697e41
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

## Candidate rebuild contract refresh

Refreshed the command fingerprints after the base-moved candidate rebuild fix
and its conflict outcome correction. The source, rules, steps and decision
table are unchanged; only the live command contract pins and derived digests
changed. Exact candidate CI, repair authority and publication fences remain
server-owned. Activation remains an operator action.

## Subject table handoff

The disabled bundle includes a source-owned integration decision table.
Existing event rules remain available for feature-off rollback. Each subject
retains its exact artifact pin. Gate choices and replay deadlines are explicit;
root publication needs the observed publisher fence, distinct from repair
writer authority. Agent Queue retains authorized continuation; the generic
template retains a no-default exhaustion gate. Import/activation and production
cutover evidence remain operator-owned.
