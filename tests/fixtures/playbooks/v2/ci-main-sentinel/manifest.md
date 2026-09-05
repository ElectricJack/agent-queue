---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:0d3a6e40bca21d48426062d29104539b508eaf756d7930e5ab83a8b37b50498c
source_sha256: sha256:a81b367d27f3a27f04169282e5183ee7925d099ee4ba1113cffb9af1c62f15cd
contract_fingerprint: sha256:787834ee710a0d62d5159fd6723f67f7faf6843af65dbd561a1e31e4c75f7c5f
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
  - ensure_task
  - gate_create
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `ci-main-sentinel`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
