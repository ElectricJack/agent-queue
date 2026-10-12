---
playbook_id: stuck-task-watch
artifact_sha256: sha256:3ea1144f4896607c409e5333dc1ff0ae79babc61b3c03e2af8d42b42f4bab0fa
source_sha256: sha256:f8bcded1876b0b88ba54f174b584508e041483793cefe451e25bae1f7e6d2f5f
contract_fingerprint: sha256:d33fa4d0695ff95e51fc0ef7725f3e1f76e967d13f9615779d875308195b68f2
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - get_stuck_tasks
  - message_send
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Stuck-task watch artifact manifest

The deterministic graph reads on timer.10m, ends quietly for an empty list,
and sends every nonempty summary to session:supervisor-global. The two grants
permit observation and messaging only. No watch memory or repair capability.
