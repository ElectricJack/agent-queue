# Stop repair continuation without progress and archive obsolete delegates

Task `keen-bridge-30`, 2026-10-01. Amends continuous delivery's finite sessions,
durable work rule; explicit human gates and frozen policy snapshots remain authoritative.

A primary repair may exhaust into one debug stage. A continuous-policy debug
stage may exhaust into another stage only when its authoritative subject SHA
differs from the preceding stage's subject SHA. Generation/revision counters,
new worker identities and repeated checks on the same head are not progress.
Each allowed stage retains its pinned attempt limit and absolute deadline.

Without progress, exhaustion ends the current stage without allocating a
successor or resetting a budget. The operation remains escalated, with its
current delegate, owner, branches and workspace preserved. The stage dossier
records a stable supervisor recovery incident, the exact subject, budget,
predecessor and history identities. One durable supervisor message names that
incident and the preserved resources. Replayed evidence, timeouts, dispatches
and service ticks cannot allocate another delegate. This operational recovery
does not create a human approval gate or cancel the operation. Policies whose
exhaustion requires a human continue to use the existing human-required path.

The integration reconciler may archive generated repair delegates that are
already terminal and obsolete: their operation has ended, or every stage that
names them is terminal and precedes the active stage. A current delegate and
the parent incident remain visible. Archive uses the ordinary archive path and
preserves IDs, task comments, completion records, stage evidence and branches.
A preserved comment records the reason, stage history and task metadata before
the active row is removed.

The archive transaction rechecks obsolescence under the project lock, refuses
assigned agents, retained claims/sessions, any unreleased branch owner or locked
workspace, children, gating dependencies, open gates and unresolved candidate
reservations. All normal batch, delivery and integration removal guards still
apply. Historical stage references may permit archive of a superseded stage,
but never hard deletion. No owner or gate is released as an archival side effect.

`aq integration release-delegates OPERATION_ID --archive-obsolete` exposes this
same guarded reconciliation to local operators and project supervisors. It
reports archived IDs and named refusals, and may inspect obsolete stages of a
live operation without ending it. The command without this flag retains its
existing ended-operation settlement behavior. Repeated reconciliation is safe.

Regressions cover repeated unchanged-head timeouts and failed checks, progress
continuation, closed-writer replays, pinned human exhaustion, earlier live
stages, ended delegates, history retention, owners/claims/workspaces/gates,
dependencies, candidate reservations, and public command authorization.

## Incident evidence and remaining operational recovery

Supported `aq integration status agent-queue --control-only` reads confirmed
operation `bcab5af6-bf48-49d8-9491-c003e927e3ac` remains escalated. The
supervisor-supplied stage-6 dossier names the same `93da7393` head as stage 5,
with zero counted attempts. A later status read during implementation showed
the reserved owner had advanced again to stage 7 on the unchanged incident.
No live operation, branch owner, task, gate or database was changed by this task.

The supervisor also reported an admission failure: the repair branch no longer
descends from its frozen starting commit. `rebind-repair` cannot repair this
detached reservation: it expects `stage.trigger_id` to name a conflict intent
(later stages use `stage-exhausted:...`) and requires an already attached writer.
Recovering that admission failure needs an operation-bound, detached-writer
reconciliation with exact current remote head, applicable conflict intent and
delivery/ancestry evidence, plus owner, stopped-claim and budget checks. Existing
rebind must not be bypassed with a guessed branch reset or another repair stage.
That missing control and the evidence required were reported to the supervisor
and the `azure-impact-88` dossier task; it is separate from safe graph retirement.

Once this change is delivered and deployed, the supervisor can reconcile the
obsolete graph rows with:

```bash
aq integration release-delegates bcab5af6-bf48-49d8-9491-c003e927e3ac --archive-obsolete
```

The result reports each archived ID and each remaining refusal. It does not
repair the branch-admission failure, unblock a gate or end the operation.
