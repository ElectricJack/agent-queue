---
playbook_id: root-integration-train
artifact_sha256: sha256:a93c3c32e05bdff64f25279531b6eaf04aba01e8bbd48a8125230a1ca32fa160
source_sha256: sha256:ec7ccbab0f601d847c59d118f4fac426f5c33951a007f731a5b2935f4eed9c1b
contract_fingerprint: sha256:7b6380af1d3d88ba850d53910ba311774cfe2b802e8eb3fafe2a624bd4b74b9e
questions_resolved: []
capabilities_granted:
  aq_commands:
    - integration_build_candidate
    - integration_ci_evidence
    - integration_cleanup
    - integration_promote_main
    - integration_release
    - integration_repair_dispatch
    - integration_repair_start
    - integration_seal
  plugin_tools: []
profiles_referenced: []
---

# Review decision

Approved as a disabled offline-reviewed artifact. Every command carries only a
durable subject or the existing sweep request identity. Git, forge, CI, lease,
fence, policy, repair, and cleanup authority remain server-derived. No
activation is created; Task 11 owns operator activation.
