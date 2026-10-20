---
playbook_id: root-train
artifact_sha256: sha256:92617fde23736f359110002c87df676deadc3b70f01e8faad754e8ead2616196
source_sha256: sha256:a42198058ea2c14d0a3d92451bbb64861cd5953856ad1ab078060a7fb563ce0e
contract_fingerprint: sha256:365c5f42781909921f64f205484f5fad3df3e340f07184b5f84b4bc44767ddc2
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - integration_cleanup
  - integration_release
  - integration_repair_dispatch
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

## Candidate construction contract refresh (2026-10-04)

Rebuilt with `scripts/rebuild-reviewed-playbook-artifacts.py` after the
`integration_build_candidate` execution fingerprint changed. The semantic diff
retains every rule, step, transition, capability, source hash and integration
decision table. Only that command's compiled fingerprint and compile timestamp
changed; the artifact digest and aggregate contract fingerprint follow them.
No compiler questions remain. Installed activation and frozen operation pins
are still owned by the operator and daemon.

## Publisher retirement repair (2026-10-12)

Removed event rules and recorded grants for retired publisher commands. Candidate
construction and publication belong to the Git train. Retained recovery, release
and cleanup rules compile against the live registry; the authored integration
policy tables are unchanged. The refreshed digest requires operator review
before activation.
