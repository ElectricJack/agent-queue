---
playbook_id: github-issue-triage
artifact_sha256: sha256:ea303d704b7d3fc8e382aea18d087f64c19b271107b718f18a7c4eb4d0745fed
source_sha256: sha256:99d29e6c506b84f1937b1568d870d6693511c8e2cc8a8013ede8e81ee3ead7ae
contract_fingerprint: sha256:7a3dfcdf21a03f0ad11799169fd2470bac4a5aed15a7a931c89168cf007cebb3
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - github_issue_triage
  - github_issue_fix_approved
  - github_issue_rejection
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Reviewed GitHub issue triage artifact

The three deterministic rules are reviewed against the source and commands.
Import remains inactive until the agent-queue supervisor activates it after
deployment.
