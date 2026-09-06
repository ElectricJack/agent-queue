---
playbook_id: root-integration-train
artifact_sha256: sha256:facd77e38272ef61c31027cde4e0b66e84412f9703a806b53bde0fede51b9644
source_sha256: sha256:91f2a47fd934467c1392f3234385008e9c764db275362c037c31c40e1ad87d4b
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
