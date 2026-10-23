---
playbook_id: morning-report
artifact_sha256: sha256:64f34f6ebaa0e12b70ee0ed2788bd3ab879cee208165d36a8f3c5e71598f0fde
source_sha256: sha256:2305fcd35ed1adc8ffa16ab383929a00c02e739827fe421beea8eb321707f99d
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

The building-block port declares `required_capabilities: []` and makes the exact
result paths, pending predicate and timer limits explicit. The empty declaration
opts the source into the compiler's stronger policy-alignment checks without
adding trusted execution metadata. A nonempty declaration changes the invocation
principal and permits a supervisor manual run that the original policy rejects
(`test_reviewed_morning_playbook_refusal_retries_once_then_fails`). Preserve that
legacy caller behavior and the command-derived grant. The typed command,
decisions, timers and existing command fingerprint remain unchanged.
No ad-hoc scan, agent step or script existed to replace. Daily reservation,
evidence freezing, visibility, author requests and immutable finalization remain
command-owned; state/notify would duplicate their transaction and delivery ledger.
Collections, snapshot pages, doctor scans, scripts and a playbook state namespace
are therefore deliberately absent.

The pre-port artifact is retained byte-for-byte in
`tests/fixtures/reports/morning/building-blocks/legacy-artifact.json` with its
original SHA pinned by `test_morning_building_blocks_preserve_recorded_graph_and_contract`.
The adjacent `scenarios.json` drives both recordings through the real engine,
typed command and durable timers, covering pending/final states, suppression,
disabled/waiting schedules, timezone guards, rollover, transient/permanent
failures and duplicate daily events. Existing report goldens and test assertions
remain unchanged. This is a reviewable bundle, not an installation or activation.
