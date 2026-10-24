---
playbook_id: supervisor-hourly-report
artifact_sha256: sha256:e221d2ca4becab5087ffb6c1d5ec5f59edb0951648915734bb80539ae006d7ab
source_sha256: sha256:06f2d39cae7ebdd63c913e78956bf053034ed2dc408b9cb5e274076c2763601e
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
