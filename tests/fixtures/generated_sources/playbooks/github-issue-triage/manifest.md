---
playbook_id: github-issue-triage
artifact_sha256: sha256:19bf128ec51bf17251a6edbb549dabd907f9824090b14e4884b0a6367f02f961
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
