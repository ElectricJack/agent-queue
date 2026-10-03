# P0 automatic integration stall recovery acceptance

The five P0 fixes are assembled on the acceptance task branch, with real Git
origins and disposable PostgreSQL regression evidence. This report is the
handoff to the project supervisor for deployment and live observation. The
one-week observation is **pending**; immediate automated checks do not establish
that a week without repair recovery notifications has elapsed.

The approved source is `rev-agile-ridge` revision 2, digest
`5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`, section 6
phase 0. Acceptance task: `keen-ridge-24.6`.

## Candidate ancestry

Normal merges preserve the published P0 parent
`c172225d729003757d880493a25f78900785d893`, the outbox prerequisite
`159165f9e39fbf6aed71dbf011c8833bfd926353`, and compatible main
`e7b745d1e5a5fd218b84a7bf4033088ddfd17737`. The combined source base is
`79bcdef93c51030414d914657f41c8316e674f56`. Source conflicts retained both
test imports; generated files were regenerated from the combined sources.
Child merge ancestry is retained. Publication of this acceptance branch does
not itself assert its delivery to main.

| Fix | Assembled implementation | Regression evidence |
| --- | --- | --- |
| Durable outbox sinks and bounded retry age | `outbox.py`, runtime proof of no consumer, per-project `max_wait` | Frozen destinations, unavailable artifacts, custom subscribers, retry quarantine and replay; connected six-row drain after response loss |
| Writer-aware expiry and mechanical unknown dispatch | `repair.py`, durable dossiers and once-per-reason messages | Stopped progress is preserved through existing owner recovery; an unclaimed writer keeps its ordinal; human holds retain the fence |
| Bounded calls and durable refusal retries | `service.py`, parent intent pass, green continuation backoff | Hung review cancellation leaves later sources running; refused dispatch survives service reconstruction; exact green promotion keeps retrying |
| Default owner recovery and rowless empty seals | `config.py`, existing `OwnerRecovery`, scheduler | Default sweep releases stopped writers while protecting live writers; repeated empty seals leave no batch, owner, repair operation, lease or remote write |
| Review polling and warning noise | `github_review_poll.py` | Changed PRs are polled; unchanged roots do not repeat review reads or state warnings |

The existing owner-recovery implementation is reused. No replacement recovery
engine, operator database migration, daemon restart, or live task repair was
performed by this worker.

## Immediate automated evidence

Successful test invocations use the isolated maintenance DSN
`postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres`.
The harness creates and removes databases it owns. Real Git uses temporary bare
origins and worker worktrees; no hosted forge or LLM is needed.

| Check | Result |
| --- | --- |
| `aq test tests/test_integration_stall_recovery.py -x -q` | 3 passed in 13.32 seconds |
| `aq test tests/test_integration_repair.py::test_continuous_replay_never_releases_operator_held_delegate tests/test_integration_stall_recovery.py -q` | 5 passed in 17.49 seconds |
| Affected integration area listed below | Final run: 562 passed in 192.94 seconds |
| Ruff on the three acceptance and merged test files | Passed |
| `scripts/regenerate-generated.sh`, its `--check`, and `scripts/regenerate-ts-client.sh --from-file` | Completed; generated files current |

The affected area command is:

```bash
aq test tests/test_integration_outbox.py tests/test_integration_service.py \
  tests/test_integration_repair.py tests/test_integration_repair_rollover.py \
  tests/test_integration_sealing.py tests/test_integration_owner_recovery.py \
  tests/test_integration_main_promotion.py tests/test_epic_pr_review_evidence.py \
  tests/test_epic_train_end_to_end.py tests/test_selection_catalogue.py -q
```

The initial failure was
`test_continuous_replay_never_releases_operator_held_delegate[True]`. Its old
expectation permitted ownership handoff during a manual hold. The assembled P0
code refuses that handoff with `human_required`. The test now also checks the
unchanged owner row and successful dispatch only after explicit resume. The
recorded main baseline dated 2026-09-22 does not list this failure. Production
behavior was not weakened and no test was skipped to reconcile it.
The final affected area run passed all 562 tests after the correction.

An invocation without the test DSN and one with incorrect test filenames were
refused before assertions ran; neither is counted as verification. No full
suite was run. Exact-SHA trusted CI, expected-old journaled pushes, rejected
members and explicit holds remain covered by the affected area scenarios.

## Supervisor supplied deployment evidence

Source: durable supervisor message `msg-b3d5f0c807f2405c8daa6d8162b5e51e`,
received 2026-10-03, describing operator observations at approximately 07:22 UTC.
These are supervisor-supplied live observations, distinct from worker tests.

| Evidence | Supervisor report |
| --- | --- |
| Published main and running operator | Main `2a126c67`; running checkout `68319ba37` |
| Daemon and database | Healthy and ready, PID `2654752`, database head `58` |
| Backup | `pre-deploy-20261003T071932Z.sql` |
| Live outbox for agent-queue | Zero undelivered across all types; zero retrying; zero quarantined |
| Azure parent `azure-vault-92`, incident prefix `bcab5af6` | Adopted `acc7f2b0` on main `e7b745` |
| Knowledge parent `calm-grove-25`, incident prefix `338ab85e` | All six receipts, including `49288437` for child 6; fresh head `7740eb7`; named remaining validation `verifier-g5` is running |
| Outbox prerequisite | Completed through operator adoption `e8645e8e-1c90-4e6a-a30e-ef61f8f6c51f`, preserving the independently tested tree |
| Feature state | Bounded owner recovery sweep on; new reconciler shadow and active flags off |

The Knowledge incident has a named remaining validation gate, `verifier-g5`.
The supervisor owns its final validation/disposition receipt. That remaining
validation is separate from the original integration stall. The receipt and
deployed SHA for this acceptance branch must be added by the integration owner
after publication; the above snapshot does not claim that they already exist.

## Pending one week observation

Owner: project supervisor. Window: **2026-10-03 07:20 UTC through
2026-10-10 07:20 UTC**. Status: **pending**. The supervisor explicitly supplied
the window and retained live rollout ownership. No worker session needs to
remain occupied for this observation, and later-phase implementation can proceed.

| Observation | Current evidence | Completion condition |
| --- | --- | --- |
| Repair recovery notifications | Full-window count pending | No `Task recovery: repair-…` notification throughout the complete window, with continuous message/log coverage |
| Outbox backlog | Zero at the approximately 07:22 UTC snapshot | Timestamped samples continue to show zero undelivered; any quarantine is reported as undelivered |
| Successor allocation on unchanged heads | Automated cases retain ordinals 0 and 1 | Audit new allocations by subject head during the window; investigate any allocation beyond the two-successor bound |
| Parent incidents | Azure adopted; Knowledge at named verifier gate | Final delivery/validation receipts or an explicit named human gate |
| Rollout identity and rollback | Supervisor owns rollout; reconciler flags off | Record deployed SHA, policy artifacts/generation, feature state and rollback decisions at every change |

At the end, record the actual UTC start/end, coverage gaps, notification count,
outbox samples, stage allocation exceptions and parent dispositions. A quiet
sample or a gap in retained logs cannot establish the full-window result.

## Operator audit and rollback

The supervisor/operator can take read-only snapshots using
`aq integration status agent-queue --control-only --json`. The SQL below is
for the operator's normal read-only connection; this worker did not open that
connection. Retain the complete results with UTC timestamps.

```sql
SELECT event_type,
       count(*) FILTER (WHERE delivered_at IS NULL) AS undelivered,
       count(*) FILTER (WHERE delivered_at IS NULL
         AND last_error LIKE 'retry_budget_exhausted:%') AS quarantined,
       count(*) FILTER (WHERE delivered_at IS NOT NULL
         AND last_error LIKE 'unsubscribed:%') AS audited_sinks
FROM integration_outbox
WHERE project_id = 'agent-queue'
GROUP BY event_type ORDER BY event_type;

SELECT id, created_at, subject, body_kind, body
FROM messages
WHERE project_id = 'agent-queue'
  AND created_at >= extract(epoch FROM timestamptz '2026-10-03 07:20:00+00')
  AND created_at < extract(epoch FROM timestamptz '2026-10-10 07:20:00+00')
  AND (subject LIKE 'Task recovery: repair-%'
       OR (body_kind = 'task_recovery' AND body LIKE '%repair-%'))
ORDER BY created_at, id;

SELECT operation_id, ordinal, starting_sha, started_at, repair_task_id,
       attempts, state, current_subject, dossier
FROM integration_repair_stages
WHERE started_at >= extract(epoch FROM timestamptz '2026-10-03 07:20:00+00')
ORDER BY operation_id, ordinal;
```

Correlate stage allocations with the exact current subject and dossier
allocation SHA. Historical high ordinals from before rollout must remain
auditable; do not delete them to make a count pass. Zero eligible retry rows
does not mean zero undelivered rows: quarantine deliberately keeps undelivered
payloads for repair and supported replay.

For rollback, retain the backup, policy snapshots, refs, outbox destination
manifests and promotion journals. `integration.owner_recovery_sweep: false`
disables the default automatic owner sweep. Keep new reconciler flags off.
If publication must pause, use the supported project mode transition after
reading its current generation:

```bash
aq integration enable agent-queue --mode observe \
  --expected-generation <observed-generation> --reason 'P0 rollback investigation'
```

Honor any drain or outstanding-operation refusal rather than bypassing it.
An operator loading a tested checkout uses `aq restart --no-dashboard` and
verifies readiness and live supervisor adoption. Application and migration of
the operator database remain operator actions. Do not start another publisher,
rewrite main, clear journal rows, or lift a rejection/hold as a recovery step.
