---
playbook_id: supervisor-hourly-report
artifact_sha256: sha256:f4ceb5bfb2e4ffc3f5b7610c6e46977e3a268931e6b967121f9cdd38ef599207
source_sha256: sha256:a3f51598421e46e5864ad64758b32cfa5190ec1b4d3debc9a653478744cf0a1f
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
