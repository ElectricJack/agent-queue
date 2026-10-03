---
playbook_id: root-train
artifact_sha256: sha256:6681dd209a52e011b7f8f49f32a6d952782ec64a5e3df68da2ab7a34a01a25cf
source_sha256: sha256:723b8dca06fb081ca3daf410e2eeeaa382d515bb84b6968f0a4e5fe516b2e07b
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

System-scoped shared root route for any project's train policy. The graph is
the reviewed `agent-queue-root-train` graph unchanged, built by the same
reviewer-authored body over the shared source at system scope. A system
activation serves only projects whose frozen policy route names this playbook
(`INTEGRATION_LIFECYCLE_PLAYBOOK_IDS`, `src/playbooks/services.py`). Commands
receive durable subject identities; CI trust, Git refs, repair budgets, and
promotion authority stay server-owned. Import and activation require operator
review. The bundle has no automatic activation.

## Subject table handoff

The disabled bundle includes a source-owned integration decision table.
Existing event rules remain available for feature-off rollback. Each subject
retains its exact artifact pin. Gate choices and replay deadlines are explicit;
root publication needs the observed publisher fence, distinct from repair
writer authority. Agent Queue retains authorized continuation; the generic
template retains a no-default exhaustion gate. Import/activation and production
cutover evidence remain operator-owned.
