---
playbook_id: morning-report
artifact_sha256: sha256:7c45a735f4f1618ac7ed0247e1098611595ac3b1c9b70abdacbaf1e6a16a5ecd
source_sha256: sha256:fc6f075eb1485241fc79d953655ebf647db5f0804f61431b9dee297b7404aed3
contract_fingerprint: sha256:15b5ac5c8f0a02fd60511475df6f1a12f40b067aed9ec87d3210b9dea8c6d226
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - morning_report_tick
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `morning-report`

The deterministic recording contains one system minute-tick command and two
terminals. It grants only `morning_report_tick`, references no profile, and
contains no model, worker task or transport step. Structural and contract checks
validate import. The operator enables this optional policy; shipping does not
add it to required or default activations.
