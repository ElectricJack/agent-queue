---
playbook_id: root-integration-train
artifact_sha256: sha256:997b5af3cf9eab5630e0e71df55f2a62a039b892c74128f9e9779b763cc9dac9
source_sha256: sha256:ded4a3cb7b9e8cff19a8eeca165703e719931c6afcd20e10c54dde3af60278e8
contract_fingerprint: sha256:f3ab93c5b413989823828cf25577bd587388f9525ba79b80e7126fa5fa8e1907
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
