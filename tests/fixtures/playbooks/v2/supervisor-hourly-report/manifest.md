---
playbook_id: supervisor-hourly-report
artifact_sha256: sha256:a9f4daeb8ff4385ab90e943438d572971adb4e5e21f141cf8b9c319e65457fee
source_sha256: sha256:d70f34a214154f118187435955fab062a85c9cfba80fc062676cb2cc0a61513d
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
