---
playbook_id: supervisor-failure-triage
artifact_sha256: sha256:6e21cb2f908e13e8cb637c95a653c35e8dc691a22c8428f649e249f8f7d7f241
source_sha256: sha256:5952c8dd787e58fef74dfc2262c650dfccec1dc21f221dc0b23344c2b9be2f20
contract_fingerprint: sha256:507b276395730700180ed27368e44fb585f4da43a665f8dca3cd5835f105622a
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - task_failure_triage_notify
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `supervisor-failure-triage`

This manifest binds the reviewed strict-hold failure-triage policy to the
command contract that creates or reuses its one durable incident.
