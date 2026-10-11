---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:e54de329fda3b12177b48cfb6d88090cbd9b623245cbddb43ec6178244c8f913
source_sha256: sha256:4a490651f6c86fb7cef3a1be29f94e5708795736977bc94cb2d39c17d6adf1a5
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

## Subject table handoff

The disabled bundle includes a source-owned integration decision table.
Existing event rules remain available for feature-off rollback. Each subject
retains its exact artifact pin. Gate choices and replay deadlines are explicit;
root publication needs the observed publisher fence, distinct from repair
writer authority. Agent Queue retains authorized continuation; the generic
template retains a no-default exhaustion gate. Import/activation and production
cutover evidence remain operator-owned.

## Candidate construction contract refresh (2026-10-04)

Rebuilt with `scripts/rebuild-reviewed-playbook-artifacts.py` after the
`integration_build_candidate` execution fingerprint changed. The semantic diff
retains every rule, step, transition, capability, source hash and integration
decision table. Only that command's compiled fingerprint and compile timestamp
changed; the artifact digest and aggregate contract fingerprint follow them.
No compiler questions remain. Installed activation and frozen operation pins
are still owned by the operator and daemon.

## Dashboard check-set compatibility (2026-10-04)

The subject table and its CI observation action now pin the sixteen-check
`ci-4f7c710bba01` set already required by the repository workflow, trust manifest
and project policy. The added check is `Dashboard (typecheck/build)`. Rebuilt
with `scripts/rebuild-reviewed-playbook-artifacts.py`; no compiler questions
remain. Existing subjects retain their frozen artifacts and policy snapshots.
