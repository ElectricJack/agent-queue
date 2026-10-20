---
playbook_id: stuck-task-watch
artifact_sha256: sha256:91774e5772d5e170e0f8d7c51f217a1ce5f3de4dd8ba86c0617ea9bc8754d8cf
source_sha256: sha256:f8bcded1876b0b88ba54f174b584508e041483793cefe451e25bae1f7e6d2f5f
contract_fingerprint: sha256:564af3103b83d1e1b6f2987477e1a09928fc4532eeaae4b971336ad2e9ba8682
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
