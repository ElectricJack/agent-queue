# Escalation lifecycle and noise, keen-forge-77

## Seven-day baseline

Supervisor snapshot: **2026-10-16 07:52 through 2026-10-23 07:52 UTC**,
project `agent-queue`. The read-only `escalation list --limit 500` returned all
229 historical rows. Counts of creations and closures use their own timestamps;
a closure can belong to an incident created before the window.

| Source | Created | Cancelled | Automatic cancellations | Manual cancellations | Resolved | Open now |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| continuation | 16 | 15 | 11 | 4 | 0 | 1 |
| gate | 4 | 5 | 0 | 5 | 0 | 8 |
| integration_delivery_stuck | 14 | 0 | 0 | 0 | 0 | 14 |
| integration_train_recovery | 0 | 1 | 0 | 1 | 0 | 0 |
| integration_train_stall | 31 | 31 | 8 | 23 | 0 | 0 |
| message | 0 | 4 | 0 | 4 | 0 | 0 |
| retained_repository_recovery | 1 | 0 | 0 | 0 | 1 | 0 |
| supervisor | 0 | 1 | 0 | 1 | 0 | 0 |
| supervisor_delivery | 2 | 0 | 0 | 0 | 2 | 0 |
| task | 0 | 3 | 0 | 3 | 0 | 0 |
| wait | 10 | 10 | 4 | 6 | 0 | 0 |
| **Total** | **78** | **70** | **23** | **47** | **3** | **23** |

The baseline has no actor field. Its legacy automatic-closure heuristic is
`terminal_evidence` containing `gate_id`, `archived_at` or `batch_id`, or a wait
note equal to `task_restarted` / `task_terminal`. Other closures count as manual.
`open_now` means `needs_human` at snapshot time. No stale transitions occurred.

The supervisor supplied the originals under `.aq/tmp/escalation-baseline/`:

- `escalations-2026-10-23T0752Z.json`, SHA256
  `88eaf5c02d1566db33db8ada6523683d13fb1c240552e25e76c0c220428e138e`.
- `report-2026-10-23T0752Z.txt`, SHA256
  `c1c62bde02edd5f4ed7a07e031e75823f43e3139210862b1f4a635e0d146833f`.

## Change and verification

The periodic resolver now reads every open incident. The explicit sweep shares
its source rules and revalidates saved decisions before applying them. Cancelled
gates, settled or removed batches, settled waits, released continuation holds and
completed source tasks close with an automatic outcome and source evidence.
Delivery watchdog incidents require their bound containment projection to settle;
task completion alone cannot retire them. Existing wait retirement and containment
transactions also record the automatic outcome.

Operational sources notify the supervisor. Reconciliation and retries preserve
that routing, including legacy queued posts. The durable explicit human-wait
conversion authorizes human delivery. Previously posted operational incidents
can still collapse when they close.

Train alerts honor progress, active repairs, deferred retries and bounded visits.
Their threshold includes twice the project's measured trailing-week CI p90 and
twice the lane's last measured visit duration, with a one-hour floor. Admission
alarms defer while matching capacity is being granted/consumed and coalesce an
open incident across branch holders sharing project, repository and profile.

Regression checks cover source recovery, replay, saved-plan revalidation,
supervisor routing, explicit human conversion and progress-aware deduplication.
Production state is never inferred from test fixtures.

Validation: **751 passed** across all six `test_escalation_*.py` modules,
wait admission/decisions/faults/hibernation/holding, integration and stall doctor
checks, the delivery watchdog and the selection catalogue. The `aq test` run
used the disposable PostgreSQL service on port 5534 and the default marker arm.
Changed Python files pass `ruff check`; `git diff --check` is clean.

## Post-delivery observation

The project supervisor owns the after snapshot and will attach it as a task
comment after deployment has had time to resolve the existing open pile. In
message `msg-7a23c52b3e0f4f79833c298f1e0a33f9`, the supervisor explicitly directed
this task to close without waiting for that production observation. There is no
post-delivery production measurement available in this implementation worktree.

Repeat the same per-source creation/closure window and preserve the legacy
classification for old rows. New closures have authoritative `outcome` values
(`sweep`, `task_terminal`, `gate_resolved`) and `automatic_rule` evidence for
operational recovery. Include these in automatic counts so an automatically
settled wait or released continuation is not mislabeled as manual. Report manual
cleanup separately; it should approach zero for the covered lifecycle causes.

Related fixes remain separate: `sharp-ember-93` owns uncertain-stop holds;
`sharp-zenith-42` owns no-op refreshes of already delivered epics. Their owners
have been asked to preserve this lifecycle and deduplication behavior.
