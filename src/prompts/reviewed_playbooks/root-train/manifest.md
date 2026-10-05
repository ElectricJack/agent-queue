---
playbook_id: root-train
artifact_sha256: sha256:f82d8f4f301f4ab6f93a30b2802c90fbeb8f0645db1ddd035e5bba72fc2f4053
source_sha256: sha256:723b8dca06fb081ca3daf410e2eeeaa382d515bb84b6968f0a4e5fe516b2e07b
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

System-scoped shared root route for any project's train policy. The graph is
the reviewed `agent-queue-root-train` graph unchanged, built by the same
reviewer-authored body over the shared source at system scope. A system
activation serves only projects whose frozen policy route names this playbook
(`INTEGRATION_LIFECYCLE_PLAYBOOK_IDS`, `src/playbooks/services.py`). Commands
receive durable subject identities; CI trust, Git refs, repair budgets, and
promotion authority stay server-owned. Import and activation require operator
review. The bundle has no automatic activation.

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
