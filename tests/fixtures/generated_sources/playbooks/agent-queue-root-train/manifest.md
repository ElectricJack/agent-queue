---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:f17f082f3e2e1aae663a61e42ca0be904e46885dcf22a6f7e19f4ffd5b0b0e0e
source_sha256: sha256:5732246f323dfa2f9ac2b8ebe1419d387fcb2f3d19e272142486c14093efa2b9
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

## Publisher retirement repair (2026-10-12)

Removed event rules and recorded grants for retired publisher commands. Candidate
construction and publication belong to the Git train. Retained recovery, release
and cleanup rules compile against the live registry; the authored integration
policy tables are unchanged. The refreshed digest requires operator review
before activation.
