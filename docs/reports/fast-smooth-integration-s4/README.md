# Fast and smooth integration: S4 verification and operator handoff

## Authority correction, 2026-10-22

The original report below records the evidence available to the S4 worker; its
classification of the later decisions as unapproved was incorrect. Jack's
2026-10-13 revision-3 playbook-policy decisions and 2026-10-20 revision-4
retrospective decisions override revision 2 where they conflict. The recorded
global disposition `msg-783a64391ca9486396693940ef3cb409` confirms that authority.
The user subsequently requested direct completion and delivery to `dev` on
2026-10-22, with the agent-queue project stopped during that work.

The accepted repair disposition `msg-6bc788cb753744729ef5354ca185620c` uses one
in-place session per batch family, dependency-closed splits, retained companion
sources and finite revisits. The old requirement for separate repair tasks,
claims and worktrees is superseded. The single-session fixtures below establish
that accepted behavior; their absence of the superseded fan-out machinery is
not an outstanding implementation requirement.

Playbook-controlled decisions, the project switch, current required checks,
Git-based reconciliation and bounded revisits are required work. Their completion
is tracked by the operator integration change rather than another review gate.
The historical measurements and test results below remain historical evidence;
this correction does not turn a failing check into a pass or attest a live cutover.

## Original verification receipt

Task `stark-willow-95.18`, 2026-10-21. Prepared source:
`3e60b49c6ec251d91862e31f6eb3c16a61e3dd44` (the completed S4 policy/docs
predecessor). The governing specification is native approved review
`rev-fair-lantern`, **revision 2**, source key `validation`, §§11, 12 and 14,
SHA256 `4518d9296649ddc2f0192ff73dd682864fb938e3d9687069282b4f6b0a86453f`.
Supervisor disposition `msg-6ea46b7503da4e408cbff30726d9ff4b` verified the
snapshot at `/tmp/aq-approved-fast-smooth-rev2.md`. The operator-vault path
`projects/agent-queue/specs/2026-10-12-fast-smooth-integration.md` has diverged;
its proposed revision 3 is document-only/in review. Global/human owners handle
that review reconciliation. This report ships deterministic validation and
proposed cutover manifests. It certifies neither whole-epic green, actual task
landing nor live activation.

The connected tests use real Git histories and disposable PostgreSQL, with
controlled CI and no LLM. The daemon swarm smoke uses its own database, home,
port and tmux socket. Live source OIDs, configuration, writers, CI trust,
deployment health and latency cohorts were not inventoried by this worker.
Every such field remains unresolved in the manifests. The owning operators
must complete the [checklist](operator-checklist.md) in their own project scope.

Results: **6 connected canaries passed; 44 close/recovery checks passed;
20 daemon smoke scenarios passed. The final repair-adjacent check passed
70 cases; two targeted snapshot checks passed. The area check had 1,454 passes and two
inherited ratchet failures.** The failures are
`test_integration_size_and_table_budget` (159,571 > 156,792 owned lines) and
`test_replacement_adds_no_new_integration_storage_category`
(`integration_close_attempts`). Neither is in the recorded 2026-09-22
45-failure baseline. The inputs were already present at the prepared source;
this task edits no production or ownership-budget input. The report retains
the failures for the existing repair/restoration owners.

## Evidence

- [Approved acceptance evidence map](acceptance-matrix.json): twelve approved rows,
  exact named test functions, semantic limits and separate proposal scope.
- [Verification receipt](verification.json): exact commands, outcomes, log
  hashes and limitations. Raw logs were retained at the paths in that receipt.
- [Canary manifests](fixture-manifests.json): full source and target OIDs
  emitted by the tests, batch IDs and check scope. These identify disposable
  fixture objects, not live repositories or recoverable production refs.
- [Ownership ledger](ownership-ledger.json): the measured source, groups,
  table count, missing inventory entries and writer-review gaps. Historical
  replay/shadow metadata emitted by the measurement tool is descriptive; it
  is not evidence that this task ran those replay suites or deployed a selector.
- [Agent Queue manifest](agent-queue-manifest.json) and
  [Matter Engine manifest](matter-engine-manifest.json): proposed operator
  inputs with explicit unresolved fields, no assumed empty writer inventory.

The first connected canary holds standalone CI, completes a prerequisite,
prepares its dependent, and queues a parallel source. It loses the in-memory
train and all notifications, then lands the standalone and one three-input
successor on `dev`, proving ancestry of every original source with an empty
receipt table. The second lands a healthy reduced batch on `dev`, reconstructs
the service from durable state, and lands the deferred bad source, its dependent
and their retained fix. The original frozen membership and repair history remain.

The remaining canaries race two sibling closes with an ambiguous successful
push response, and restart an author-resolved conflict after its parent advances.
They verify exact original ancestry, retained fixes and candidate checks at the
parent boundary. They do not pretend that a parent merge itself proves delivery
to `dev`. Adjacent completion-recovery tests cover the terminal database
transition separately.

## Acceptance matrix: approved revision 2 §14

The approved table has **12 rows**. [The exact evidence map](acceptance-matrix.json)
pins each row to named test functions and their recorded runs. Parameterized
functions include all instances in those runs. “Fixture evidence” means those
boundaries were exercised; it does not certify untested semantics or live gates.
The inherited failing checks remain failures in the verification receipt.

| Approved requirement | Named evidence and limits |
| --- | --- |
| One Git truth | Strict `test_strict_shadow_rejects_equivalent_delivery`, generation/object/fetch/shallow tests; `test_actual_admission_metrics_watchdog_and_freeze_consume_exact_audit`; connected dev flow with zero receipts. Legacy equivalence tests are compatibility evidence, not containment authority. |
| Stacked default | `test_project_policy_defaults_and_override`, retained/deleted-ref and multiple-prerequisite tests, cross-epic closure and `test_prepare_conflict_files_once_and_requires_published_resolution`; connected dependent starts on its prerequisite while predecessor CI is held. Cross-repository contracts remain holds. |
| Merge on complete | `test_clean_merge_is_two_parent_and_lost_push_reply_recovers`, conflict/budget/publisher-only tests, terminal completion-recovery/accepted-tail checks, and sibling/conflict-restart canaries. Parent ancestry alone is not dev delivery. |
| Batched dispatch | `test_idle_departure_queues_during_ci_and_departs_on_settle`, empty-clock and queued prerequisite revalidation; connected configured-dev standalone followed by exactly one three-input successor after restart. |
| Red batch | **Partial: approved semantic behavior missing.** Group/build-precedence fixtures, frozen file attribution, single-family caps/escalation, reduced batch and retained fix successor are covered. The new `test_fixer_budget_restart_escalates_once_without_refiling_or_waking[time/rounds]` proves one supervisor incident and no exhaustion wake/refiling with real Git/PostgreSQL. Per-group parallel branches, four-way fan-out cap, coverage cancellation and group merge-back/one-CI are absent from the prepared service; details below. |
| Advisory epic CI | `test_advisory_epic_checks_never_hold_completion[missing/failure]` exercises explicit required checks then removal of the required activation; snapshot checks cover existing CI defaults. Actual owner uses reviewed playbook-required checks; no unapproved policy-epoch mechanism is asserted. |
| Warm caches | `test_candidate_reuses_across_released_batches_and_shares_objects`, fixer reuse/build-output tests and `test_cache_setting_reaches_real_job_contract_and_common_wait_sample`. Live capacity and build timing remain unmeasured. |
| Speculative CI | Model refusal/eligibility tests; `test_kept_speculative_candidate_publishes_exact_sha_without_rebuild`, predecessor-change discard and `test_slow_speculative_build_never_delays_predecessor`. Controlled CI; no real hosted speculation launched. |
| Automatic epic | `test_collected_two_child_epic_gets_completion_and_lands_via_root`, frozen readiness/late/reopen/hold tests, root-and-child batch publication; connected configured-dev standalone/multi-input flow. Live review/promotion bindings remain unresolved. |
| Watchdog | Threshold floor/measured-CI tests; `test_stuck_generation_gets_one_takeover_and_one_incident_across_restart`, unknown-Git refusal and explanation diagnostic. No live explanation/incident pair is attested. |
| Metrics | Named quantile/cohort/segment tests include pending, censored, late, reopened, overlapping and no-data work; recorder failure cannot break observation. Backend evidence, not dashboard browser validation or a live SLO. |
| Migration | Cutover preview/stale/live-writer/retention tests; relocation path-family/current-Windows/dirty/restart/reverse-plan tests. Actual sources, backups, runners, native storage and product health remain operator-owned unresolved fields. |

### Original Red batch assessment (superseded above)

`src/integration/fixers.py::BatchFixerService.allocate` allocates one session
per batch family. `src/integration/fixer_runtime.py::DaemonFixerRuntime.failure_input`
normalizes and groups failures but passes them together as that session's input.
`RepairPolicy` exposes `primary_attempts`/`primary_seconds`; it has no
`max_parallel_fixers` or independent `max_rounds` field. The implemented
single-family service caps observed red rounds at `min(3, primary_attempts)`.
The time and round canaries reconstruct the service before exhaustion and each
replay, preserve original ancestry and the registered checkout, emit exactly one
supervisor incident, and create zero replacement tasks/sessions or exhaustion
wakes. Exact green still settles. Those checks **do not prove** a per-group cap
or coverage-declaration cancellation.

The substantive missing approved §14 Red batch behavior is a per-group branch
allocator with at most four concurrent fixers, cancellation when one declares
coverage of another, and merge-back followed by one CI of the combined fixes.
No such scheduling/cancellation/merge-back path was found in the prepared source.
Adding passing tests around the single-fixer implementation cannot establish it.
This validation task adds connected budget coverage and records that exact gap;
it does not expand into implementing a new repair scheduler or certify the full
Red batch row. Deterministic grouping/build precedence and attributed split with
companion repair sources are independently exercised by the named tests.

### Original revision-3 classification (superseded above)

The prior report incorrectly included six additional rows: **Policy owns
decisions; Project off/on switch; Live required checks; Green wins; Never blocked
by default; Retained reduction goals.** They are unapproved proposal rows,
separate from approved revision 2 acceptance. Missing `train_policy.py`, a
`fast-smooth-train` bundle, a sole project-playbook switch or live policy epochs
is neither a passing approved result nor an approved prerequisite blocker for
this task. No policy implementation expansion or further paperwork wait is
required. The source measurement and supervisor-approved repair/restoration
authorities below remain factual findings under their separate authority.

## Restoration and exception provenance

The prepared source has 45 tables against the unchanged transition cap of 45.
`integration_close_attempts` remains counted and visible; removal of the empty
`integration_legacy_gate_applicability` restored the count. Historical audit,
rollback evidence and receipt-retirement tests remain. Restoring the count does
not by itself resolve the new-category ratchet or the final ≤10-table goal.

The fixed baseline remains 140,499 owned lines plus the 15,000 replacement
allowance: 155,499. The manifest's existing batch-77 exception adds exactly
1,293, yielding 156,792. Its exact batch, source and decision records are in
the ledger. The previously approved batch-48 +2,166 provenance must remain
separate; it is not automatically added again or substituted for a baseline.
The current measurement exceeds the recorded cap by 2,779. This report does
not invent an allowance by adding historical deltas to that measurement.

Supervisor authorities `msg-dbb0120c6172499081721c033e894b39` and
`msg-90f9f1b1e0cd4e8e8d28071cffc927a6` permit bounded exact exceptions and
retain the final caps. The latter recognizes `integration_close_attempts` as
approved storage and permits an ordinary integrated repair to bind an exact
measured allowance with zero headroom after consolidation. The integration
repair owner must supply its source/count/batch/authority binding; this worker
has not changed ownership manifests or weakened the ratchets. Batch-56 repair
and `azure-ridge-72` retain their assigned repair/restoration roles.

The inherited ledger also reports 86 uninventoried shared definitions, 69
unreviewed writer fingerprints and 14 legacy sinks pending stage 4. These are
measurement findings, not proof of 69 live processes or permission to delete
historical data. They remain inherited source-accounting findings under the
recorded supervisor dispositions; proposed revision-3 reduction requirements
are not imported into approved revision-2 task acceptance.

There were no unexplained process stalls in the new connected canaries:
controlled pending CI and the author conflict were intentional exercised waits.
The report does not extrapolate this to the live frontier. Before cutover the
operator must reconcile withheld, fenced, frontier-excluded and awaiting-evidence
work against actual source ancestry, ownership and the shared waiting view.
