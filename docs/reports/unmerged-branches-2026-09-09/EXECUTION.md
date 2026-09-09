# Merge-plan execution — 2026-09-09

Scope: user-authorized steps 1–6. Focused local tests and an isolated daemon/CLI smoke run; no full CI wait and no branch deletion.

## Delivery checkpoints

| Step | Result |
|---|---|
| 1 — operator snapshot | Remote main fast-forwarded from e1390040 to 39783ab5; all 23 operator snapshots consolidated. |
| 2 — approved fixes | keen-forge, brisk-delta, prime-ridge delivered through 4355e121. Preserved time-window dashboard behavior, PostgreSQL-only fixtures and stronger operator handoff logic. |
| 3 — overlapping recovery branches | 22 older recovery delivery branches reconciled, preserving existing operator corrections and recording their ancestry instead of replaying superseded code. |
| 4 — pending recovery revisions | brisk-rapids, fair-willow, noble-torrent, keen-bridge, bright-journey, fresh-rapids and smart-horizon integrated with corrections; delivered through 00700095. |
| 5 — CLI aggregate | keen-harbor with all 16 children, aggregate CI repairs and fleet-glacier audit correction delivered through 00700095. |
| 6 — Discord aggregate | All 11 implemented child tips combined with final acceptance evidence. Published on main c9834ef9; runtime and acceptance reconciliation below. |

## Corrections made during integration

- Retained terminal-claim preservation while adding exact observed task-status/epoch guards.
- Combined failed-close cleanup with verifier requeue ownership release; stranded-fence doctor remains report-only.
- Persisted new root branch identity only after resolving its origin from the existing default branch, in the same transaction.
- Preserved operator container materialization, current-incarnation checks, manual pauses and stronger historical-workspace proof paths.
- Held orphan workspace exclusion through stopped-provider/Git proof; retained successor refusal.
- Required exact composer equality for durable AQ prompt recovery, including edited prefix/suffix and continuation cases; recognized the bounded Codex footer separately.
- Reconciled malformed-resolution recovery with the existing durable pre-push marker, preserved old evidence, required a quiescent writer, and rebound the exact stage trigger to the successor intent.
- Moved malformed-resolution recovery to migration8, then Discord persistence/comment/action migrations to9/a/b; retained operator revisions5/6/7.
- Preserved CLI JSON envelopes while removing retired per-project Discord channel fields.
- Regenerated Python and TypeScript API clients offline; recorded generated output remained consistent.
- Updated the isolated CLI bootstrap after removal of --no-auto-create-channels.

## Local validation evidence

Commands used the private PostgreSQL helper, which invokes resource-gated `aq test` from the execution worktree. No operator database was used for tests.

| Area | Result |
|---|---|
| Approved-fix backend subset | 145 passed |
| Approved-fix dashboard subset | 23 passed |
| Recovery area integration | 365 passed initially; 13 merge/test-fixture failures corrected with targeted reruns |
| Corrected recovery/CLI/root cases | 18 passed, followed by all 5 orphan recovery/race cases passing |
| Malformed-resolution recovery and live-writer refusal | 4 passed |
| CLI envelopes/options/creation + hierarchy/promotion | 272 passed initially; missing merged fixture imports corrected |
| Final hierarchy/task-graph suite | 70 passed |
| Discord lifecycle/digests/delivery/cutover | 77 passed initially; historical audit wording corrected |
| Corrected Discord docs + generated API contracts | 18 passed |
| Discord messaging/inbox dashboard | 9 passed |
| Candidate/conflict migration schema | 2 passed |
| Escalation actions upgrade/downgrade after DSN normalization | 1 passed |
| Final isolated daemon/CLI end-to-end smoke | 14/14 scenarios passed; private daemon stopped and database cleaned |

These are separate runs, with overlapping cases; they are not a unique-test total or a claim that the full repository suite passed. Whole-file Ruff exposed existing broad lint debt; targeted undefined-name checks and merge whitespace checks were used to catch integration mistakes.

## AQ/runtime reconciliation

- Delivery comments recorded for the initial approved/reconciled tasks with main checkpoint4355e121.
- Stopped the stranded root-batch and Discord repair sessions through public instance-fenced AQ commands; stopped state confirmed.
- Public candidate recovery rejected frozen resolutiond3b49b4e-3651-50cf-94ee-c6650ac6687f with invariant `contained_source_repair_changes_the_candidate`, preserving the pushed ref as audit evidence.
- Final implementation delivered to local and remote main at `c9834ef9`. All 93 delivered origin refs are ancestors; five held/internal refs remain outside main.
- Public operator `aq db upgrade --yes` succeeded; `aq db current` confirms stamp and head `a0000000000b`. `aq restart --no-dashboard` restarted daemon PID2050879; `aq status` responds.
- Public malformed-resolution recovery succeeded: old intent `intent-2f290cf6-dcc6-507d-b337-13c5f80cf12f` superseded by `recovery-c881917a-d845-5d84-870f-7880997a06a6`, receipt `receipt-d1f37c15-6c4a-51d3-bb1e-11971866b163`. No remote write was invented or forced.
- `aq digest status` confirms configured single channel, enabled 60-minute digests and escalations, cutover complete, 101 retired task threads, no cutover conflicts, no settings errors and no pending escalation deliveries. Legacy per-project settings are ignored with an explicit warning.
- Both older epic repair operations remain `human_required`: public safe abort returned `ambiguous_external_write` for CLI operation `81d0aaee-0c3a-482c-b04c-d3afe6631cbe` and Discord operation `640ced19-c324-4f6f-b50d-8f0181d181b7`. Their parent tickets remain BLOCKED despite manual source delivery. No automated promotion/CI receipt was fabricated and no guarded state was overwritten.
- Acceptance report for `noble-ridge.12` is delivered and automated checks pass, but its normal passing close refused `Managed hierarchy producer is not bound to its current checkpoint`. The task is left BLOCKED with the evidence and refusal recorded, pending canonical checkpoint reconciliation.
- Final delivery evidence is recorded as AQ task comments for the recovery tasks and CLI/Discord epics. This distinguishes main delivery from still-open automatic integration bookkeeping.

### Runtime issues still visible

The restart is successful but overall AQ health is degraded. The post-restart doctor found nine apparently stranded integration owners, one stale pool binding (`fresh-rapids`, session `3c9330f6-8780-4012-b0d4-3868326cd505`), and its integration operational check timed out after five seconds. Logs also show existing branch-materialization conflicts for `aq/nimble-apex` and `aq/clear-vault`. Other warnings include operator harness/profile/skill drift, a malformed intelligence-class file, stale supervisor playbook activation, and the separate Matter Engine `aq/nimble-dune` branch. These are unresolved operational follow-ups; this merge delivery is not a claim that the broader reliability/throughput goal is complete.

## Remaining branches outside the approved delivery list

The final execution tree leaves five origin refs ancestry-unmerged: two internal batch/repair audit checkpoints, aq/prime-ridge-wip, aq/sound-current.5 and aq/wise-lantern. They were held by the reviewed plan and have not been deleted or silently marked delivered. Matter Engine and darcyle upstream branches were not merged by steps1–6.
