# Git-first backlog settlement: operator evidence checklist

Use this checklist for `bright-rapids-84.13` after the project's live canary.
The approved vault sources are
`projects/agent-queue/specs/2026-10-04-git-first-integration-train.md`
(Problem, Migration, Acceptance) and
`projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md`+(sections 4, 6 and 7). Activation is covered by the
[train runbook](git-first-train-runbook.md). The
[2026-10-05 worker report](../reports/git-first-backlog-settlement-2026-10-05/README.md)
records reconstructed replay results and the missing operator inputs.

Only the authorized supervisor or operator captures unheld live tasks, changes
configuration or lets the live train settle the backlog. Workers consume supplied
sanitized evidence, build replay artifacts and report gaps. A worker's published
branch proves neither activation nor delivery of an unrelated task.

## Before observing settlement

1. Record the UTC window, observing operator, project, running checkout, configured
   integration mode, `integration.git_first` selection, installed policy/bundle pins
   and live-canary evidence. Use the actual mode; a development replay does not prove
   hosted delivery. An unavailable canary or inactive protocol remains an unmet input.
2. Identify current instances of all six rows below using the operator's task
   surface. The two family labels are not task IDs: enumerate their actual task/ref
   instances. Preserve dated incidents separately from current state. If an incident
   already settled, cite its observed publication and transition rather than
   recreating it. Do not assign it a fabricated before-state.
3. Start or verify retention of a structured command-dispatch audit for the entire
   observation window before settling work. Preserve rotations, restart boundaries,
   failures, refused attempts and commands with no project attribution. Verify
   coverage before using a zero count. The seven-day post-cutover observation remains
   a separate epic requirement.

## Per-item capture

The following commands are **operator-only examples**. Set `task_id` to each actual
incident and `source_ref` / `candidate_ref` / `target_ref` from its observed train
inputs. Run Git reads in the operator's authorized repository.

```bash
aq --json integration status agent-queue
aq --json task show "$task_id"
aq --json task explain --task-id "$task_id"
git ls-remote origin "$source_ref" "$candidate_ref" "$target_ref"
```

Save the full JSON envelopes and raw remote-ref results with UTC timestamps and
file hashes. Capture both before and after the train visit. Record one evidence
row per concrete task/ref instance:

| Field | Required observation |
|---|---|
| Identity | Family, actual task ID, completion/claim identity, project, repository and configured mode |
| Source | Ref, immutable completed source OID, source base OID and every required child/member source |
| Candidate | Ref, OID and tree OID; explicitly explain absence if no batch was necessary |
| Target | Ref and OIDs before/after; connect publication to the observed candidate or delivery proof |
| Checks | Required check names, exact checked OID, producer/trust identity, run/attempt, conclusion and observation time |
| Review | Required review policy, tree OID, reviewer authority and verdict; explicit policy evidence if no review is required |
| Lease | Ref, holder/session incarnation, fence and expiry; valid competing leases remain binding |
| Intent | Explicit task/project/batch holds, rejection and abort/pause intent with their owners |
| Transition | Task states before/after, timestamps and ordinary train publication/completion evidence |
| Outcome | Resolved, real conflict, unavailable input, explicit hold or pending observation; link evidence paths/hashes |

Use a fetched, consistent OID pair for ancestry or whole-source proof. Ref names
alone and a historical "15/15 green" label cannot establish exact candidate checks.
If a ref moves during capture, record that movement and capture the new pair; do
not combine checks from one candidate with publication of another. Where squash
delivery is involved, preserve source/base provenance and the whole-source proof;
one matching patch from a multi-commit task is insufficient.

## Checklist for each recorded stall

| Recorded item | Operator checks | Settlement evidence or unresolved reason |
|---|---|---|
| `crisp-horizon-90` | Enumerate all completed children and check containment in the fetched epic head; observe review/check policy for its destination | Each child's delivery, epic publication and ordinary task transition; no delegate-close audit repair |
| `vivid-quest-44` | Capture the repair's start/current OIDs and `rev-list` progress, leased target and open repair task/attempt | Added commits, expiry or valid competing lease, exact repaired checks and publication; no cumulative repair-list edit |
| Epic verifiers (enumerate instances) | Capture epic/candidate tree, required review and authorized verdict, required checks and children | Completion keyed to the observed tree; missing review/check input stays unmet without a generation record |
| Stale branch owners (enumerate instances) | Capture ref lease, session incarnation, expiry and target OID; retain any unsaved source ref | Expired lease becomes available through the train; valid live lease or unavailable source is recorded explicitly |
| `brisk-beacon-64` | Capture exact current head, child/source containment, required checks, review/holds and valid competing lease | Green publication and task transition without allocating a needless repair or clearing a stored no-progress counter |
| `swift-delta-90` | Independently capture its epic/current head, every child source, required checks, review/holds and lease | Its own publication and task transition; another green epic cannot substitute for this instance |

Let the configured train observe Git, checks, review and intent. Follow the
ordinary repair task it allocates for a real conflict or red candidate. Record a
missing source, unreachable provider, missing/changed-tree review, valid competing
lease or explicit hold as its actual blocker. Do not remove a hold/rejection to
obtain a pass, clear integration records by hand, allocate duplicate recovery
tasks or invoke any removed control.

## Replay the supplied captures

The capture format and directory layout are documented in
[`tests/fixtures/integration_replay/README.md`](../../tests/fixtures/integration_replay/README.md).
The operator supplies sanitized `remote.bundle` and `capture.json` together; keep
capture time, observer, original evidence hashes and sanitized artifact hashes in
the observation report. Include every required source/base and target object.
Workers never extract unheld task rows to manufacture a capture.

Run on disposable PostgreSQL with the normal test-slot wrapper:

```bash
AQ_REPLAY_EVIDENCE=/tmp/aq-backlog-replay \
  aq test tests/test_integration_replay.py -q -rs --tb=short
aq test tests/test_integration_train.py tests/test_integration_train_sources.py \
  tests/test_integration_root_scenarios.py tests/test_selection_catalogue.py -q
```

The replay module exercises reference train seams with real temporary Git remotes;
the area checks separately exercise the production train. Preserve emitted
`report.json` and case files, including asserted remote OIDs, task counts and
command IDs. A reconstructed pass or capture round trip does not settle a live
incident. Missing historical captures skip with `GAP` and remain unmet.

## Structured command audit

Match **exact CommandHandler IDs** against `REMOVED_CONTROLS` in
[`tests/test_integration_replay.py`](../../tests/test_integration_replay.py).
The worker report includes the sorted set and its SHA-256 so observations pin
the list they used. `RETIRED_CONTROLS` only identifies absent handlers for the
test guard; those IDs still belong to the prohibited invocation set.

Preserve each audit entry's command ID, timestamp, event/sequence identity,
project/task/session attribution and result. Count refused and failed invocations
as well as successful ones. Attribute service commands and entries without a
project ID explicitly rather than dropping them. Report command counts, exact
removed-ID matches and unmatched/unparseable rows, plus raw export paths/hashes,
window, workload and coverage gaps.

`CommandHandler.execute` emits `command.invoked` with `command`, `ok` and
redacted context when `events.command_invoked_enabled` is enabled. Emission is
best effort; the event bus itself does not persist those events. A recent-events
query alone therefore cannot certify a complete dispatch window. A retained
structured dispatch log or continuously retained event capture must establish
coverage, including restart/reconnect/rotation gaps. Command-bearing JSON log
records can be correlated, but count dispatch records once rather than counting
every correlated log line as another invocation.

The normalized report shape below is a report format, not a new runtime record or
recovery control. `null` means unobserved; an empty list with complete coverage
means no matched invocation.

```json
{
  "scope": "operator_live_settlement",
  "window_start_utc": null,
  "window_end_utc": null,
  "configured_mode": null,
  "raw_exports": [],
  "coverage_complete": false,
  "coverage_gaps": ["operator dispatch export not supplied"],
  "normal_workload_observed": null,
  "removed_command_ids_sha256": "copy from the worker report",
  "command_counts": null,
  "removed_invocations": null,
  "unparseable_rows": null,
  "verdict": "unmet"
}
```

Compare the normalized `command_id` of every retained invocation to the pinned
set. Zero matches passes only the audit requirement when coverage and attribution
are complete. It does not prove source containment, green checks, tree review or
task completion.

## Close-out

Attach each resolved instance's full before/after evidence and the structured
audit report to the existing settlement node. List real conflicts, unavailable
inputs and explicit holds individually; include an empty category only if it was
observed to be empty. The six family rows need an operator-supplied current
inventory even when no live instance remains.

Keep unsatisfied required cases unmet. Worker preparation alone does not warrant
a settlement pass. The operator owns the live action and remaining evidence;
do not file a duplicate settlement task. Preserve the independent canary,
seven-day observation, retirement and adversarial-review requirements on the epic.
