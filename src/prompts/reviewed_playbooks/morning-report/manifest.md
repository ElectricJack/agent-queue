---
playbook_id: morning-report
artifact_sha256: sha256:584735bfad6aa6df0da33f112e62e176b61e039c3071deca86aba6f1c2021195
source_sha256: sha256:23cea13fc31dfa06ee58109d9cf3efcf28e1448cc7de0d76eb680038302cb108
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

The deterministic recording triggers on `report.morning_due`, which the daemon
emits once per local day with a per-day event id, so dispatch dedup holds it to
one run per day. The run calls `morning_report_tick`, settles on the event's own
report, and loops through a 60-second durable timer while that report is pending;
a failed call gets one timer-spaced retry before the failed terminal. It grants
only `morning_report_tick`, references no profile, and contains no model, worker
task or transport step. Structural and contract checks validate import. The
operator enables this optional policy; shipping does not add it to required or
default activations.
