---
playbook_id: memory-consolidation
artifact_sha256: sha256:c69b44e5af6bf80fe9344ee7480969acf84c32c6b4e7baca43c16a27970230ff
source_sha256: sha256:397d8826c2559f3c083b00ccd044f93a545690d7989410b4d8fe6b1b4139e9e5
contract_fingerprint: sha256:90d67fce3cb16821f9b06a366068d89f78230bcecaabe95fa61e4e7f7f187071
questions_resolved: 3
capabilities_granted:
  aq_commands:
  - create_task
  - list_projects
  - render_prompt
  harness_tools: []
  plugin_tools:
  - count_project_memory_files
  - read_project_memory_file
profiles_referenced:
- supervisor
---

# Playbooks V2 artifact manifest — `memory-consolidation`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
