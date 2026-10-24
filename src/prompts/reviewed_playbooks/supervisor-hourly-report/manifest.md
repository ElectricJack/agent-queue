---
playbook_id: supervisor-hourly-report
artifact_sha256: sha256:346be6b68d84fc1c13e46ec25dcee2429f61ff40c9a814bd778d9cb27bbb5093
source_sha256: sha256:ab4e2ecbb0a8198f723c51c200b59f7b552f84e6fa995ecd989491b6e6e4a564
contract_fingerprint: sha256:0b36836ac0368d1645c843d6e57333d33b4b2bfd5f4cd970da6d5aa366410177
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - report_reconcile
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Optional hourly narrative policy

Approved design: supervisor narrative updates (rev-vivid-grove), plan (2).
Two deterministic command rules request durable reserved windows on window-ready
and minute reconciliation. No LLM or worker step; no arbitrary destination.
The sole grant is report_reconcile. Disabled by default and excluded from
required/default activation; deployment enables it after narrative sample review.
