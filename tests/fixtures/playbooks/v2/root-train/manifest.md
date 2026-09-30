---
playbook_id: root-train
artifact_sha256: sha256:569738076ce63a7b91173b7aaa91b86e81818cd70951b82634b1331644ce550c
source_sha256: sha256:9714e0d25da8df79d9b2e713b10746a623d1e2f979f10238691d19147b28d485
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
