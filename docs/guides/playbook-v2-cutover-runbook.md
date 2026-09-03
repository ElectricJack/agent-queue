# Playbook V1 → V2 cutover runbook

Operator guide for the commands that move a fleet off the V1 playbook runtime.

> **Scope.** This covers the **drain** — Package 7 commit 1, which has shipped.
> The switch itself already works via `playbooks.v2_engine`. The rollback
> observation window (commit 3) and the V1 runtime deletion (commit 4) are not
> yet implemented; the sections below say so where it matters.
>
> Design: [`docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md`](../superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md).

## Why a drain needs commands at all

Two facts about V1 decide the whole shape of this procedure.

**A `running` row can outlive the process that owned it.** Run state lives in
the durable `playbook_runs` row *and* in `PlaybookManager._running`, an
in-memory `dict[str, asyncio.Task]`. Runs are dispatched fire-and-forget and
nothing reconciles the two at startup, so a daemon restart mid-run leaves a
`running` row nothing will ever move to a terminal status. **You cannot reach
zero active runs by waiting.** Any fleet with history probably already has
orphans, and they need a deliberate write.

**The general-purpose cancel cannot stop a live run.** `cancel_playbook_run`
writes `cancelled` to the row but has no way to signal the coroutine. A live
run finishes its current node and its next persistence write puts the status
back to `running` — so a drain built on it reports zero and then watches the
count climb, with nothing in the logs to say why. Use `playbook_v1_run_cancel`
during a drain; it joins the cancelled task *before* writing the terminal row.

## The commands

All ten work while `playbooks.enabled=false`. That is deliberate: a fleet
that paused the subsystem with runs still `running` is exactly the one that
needs draining. All ten are operator-only, and every write takes a mandatory
`--reason` of at least 10 characters, stored verbatim in an append-only audit
table.

| Command | What it does |
|---|---|
| `aq playbook v1-drain-status` | Every non-terminal V1 run, classified `live` or `orphaned`, with the options for each |
| `aq playbook v1-admission-close --reason TEXT` | Refuse new V1 runs. Paused runs stay resumable |
| `aq playbook v1-admission-open --reason TEXT` | The inverse. Refused while the fleet is on V2 |
| `aq playbook v1-run-cancel --run-id ID --reason TEXT` | Cancel one run, live or orphaned |
| `aq playbook cutover-gate-status` | Readiness table, the G1 sign-off and the G2 signatures on record, and what still blocks the switch |
| `aq playbook cutover-drain-signoff --signed-by NAME --reason TEXT` | **Gate G1.** A named human signs off the drain, after the command re-verifies readiness |
| `aq playbook cutover-authorize --role author\|release_operator --signed-by NAME --reason TEXT` | **Gate G2.** One named human authorizes the switch in one role; two different people are needed |
| `aq playbook cutover-switch --to v1\|v2 --reason TEXT` | Move the fleet between runtimes. `--to v2` is refused without G1, G2 and a clean readiness table |
| `aq playbook cutover-window-status` | The acceptance table and the observation window |
| `aq playbook cutover-window-close --reason TEXT` | Close the rollback window. Not yet usable — see below |

## Procedure

### 1. Look before you close

```bash
aq playbook v1-drain-status --json
```

`live_count` is runs a coroutine still owns — those can finish on their own.
`orphaned_count` is rows nothing owns; **waiting will never clear them.** Each
entry carries `options`: `wait` (live only), `resolve` (paused only, routes to
`resume_playbook` with the human's input), and `cancel` (always).

### 2. Close admission

```bash
aq playbook v1-admission-close --reason "draining v1 ahead of the v2 cutover"
```

This sets `playbooks.v1_admission: closed` in your config and applies it to the
running daemon, so it takes effect without a restart. New V1 runs are refused
at the orchestrator trigger, at assignment routing, and at `run_playbook`.

**Resume paths are deliberately untouched.** A V1 run that is paused waiting on
a human must still be resumable, or the switch strands it and a later
`drained: true` is retroactively false.

Close admission *before* reading the drain as a gate. `drain_status` is a
snapshot; a run can start a millisecond after you read it. Closed admission is
what actually stops that, which is why `drained` requires both.

### 3. Reach zero

For each remaining run, either let a `live` one finish, `resolve` a `paused`
one, or cancel it:

```bash
aq playbook v1-run-cancel --run-id <id> --reason "orphaned by a daemon restart"
```

A cancel that cannot stop the coroutine within 30 seconds **fails and leaves
the row untouched**, rather than reporting a half-cancelled run as drained.

Cancelling a paused run resolves nothing upstream: whatever asked the human is
still unanswered. `v1-drain-status` reports `waiting_for_event` and
`current_node` per run so you can see what you are abandoning.

Re-read until `drained: true`.

### 4. Check readiness

```bash
aq playbook cutover-gate-status --json
```

Four checks, each recomputed from source on every call and each fail-closed
(an evidence source that cannot be read blocks; it is never treated as fine):

| Check | Passes when |
|---|---|
| `drain` | `v1-drain-status` reports `drained: true` — admission closed and no active run |
| `cutover_report` | `playbook cutover-report` has `blocking_reasons: []` and `rollback_ready: true` |
| `activations` | every enabled activation's health is `ready` against the live command and profile contracts (a drifted contract fingerprint reports `stale_contract` and blocks) |
| `pending_events` | zero unresolved pending V2 events |

`ready: true` means the table is clean. `can_switch` also needs the two human
gates below.

### 5. Gate G1 — sign off the drain

```bash
aq playbook cutover-drain-signoff --signed-by "Alice Example" \
    --reason "drain reviewed against the gate status of <date>"
```

The named release operator attests that the drain is complete. The command
re-runs every readiness check itself and refuses while any one blocks, naming
it; it does not trust the status you just read. It records a `drain_completed`
event carrying the name, the readiness table it verified and the V1 latency
baseline the acceptance gates are anchored to.

`--signed-by` is an attestation, recorded next to — not instead of — the
server-derived `actor`. The loopback CLI has no user identity, so the name is
what the audit trail has to say who signed.

A second sign-off for the same attempt is refused: authorizations are bound to
the sign-off they authorize, and a fresh one would orphan them.

### 6. Gate G2 — two people authorize the switch

```bash
aq playbook cutover-authorize --role author --signed-by "Alice Example" \
    --reason "I wrote the switch change and reviewed the gate status"
aq playbook cutover-authorize --role release_operator --signed-by "Bob Example" \
    --reason "release operator for the <date> cutover"
```

One signature per role, one role per person. The same name under both roles
(compared case-insensitively, whitespace-collapsed) is refused. Each
signature is a `cutover_authorized` event naming the `drain_completed` row it
authorizes; a signature for an earlier sign-off does not count.

### 7. Switch

```bash
aq playbook cutover-switch --to v2 --reason "cutting over after a clean drain"
```

Refused unless, **re-verified at that moment**: every readiness check passes,
a current G1 sign-off exists, and both G2 roles are signed by two different
people. The sign-off is evidence about the past; the switch checks the
present, so a V1 run or a pending event that appeared after G1 blocks it. On
success it sets `playbooks.v2_engine: true` and records `switched_to_v2` with
the sign-off id, both authorizations and the readiness table it verified.

A sign-off and its authorizations cover **one attempt**. `switched_to_v2`,
`rolled_back_to_v1` and `v1_admission_reopened` each end the attempt: to
switch again after a rollback, sign off and authorize again. The rollback
happened for a reason.

## Rollback

```bash
aq playbook cutover-switch --to v1 --reason "<what went wrong>"
```

Rollback needs no gate — no sign-off, no authorization, no readiness check.
An operator must be able to roll back at 3am.

Rolling back does **not** reopen V1 admission. `v2_engine=false` with
`v1_admission=closed` is the supported rollback state: existing runs resume, no
new ones start. `v1-admission-open` is refused while the fleet is on V2 —
admission open under V2 would let a rollback silently start new V1 runs against
artifacts nobody reviewed.

You can always roll back by editing `~/.agent-queue/config.yaml` directly; an
operator must be able to do this at 3am without a gate row. That is detected
rather than prevented — `cutover-window-status` reports a runtime that
disagrees with the audit log as `runtime flipped outside the cutover command`.

Rollback stops being available once the window is closed, which is the point of
closing it.

## The observation window — not yet usable

`cutover-window-close` **will refuse today, by design.** Of the sixteen
acceptance measures, only active-V1-runs, the runtime/audit-log agreement and
the wall-clock floor are wired; the other fifteen report `pass: false` with a
`blocking` note naming the source that will supply them (Package 7 commit 3).

This is fail-closed on purpose: a gate that treated "not measured" as "fine"
would be the same silent success the whole drain design exists to prevent. Read
`cutover-window-status` for the current picture; do not delete the V1 runtime
until the window actually closes.

There is deliberately no `--force`. An operator who wants to close early edits
the config themselves and owns it, and the audit table records that they did
not use the gate.

## The audit trail

Every write appends to `playbook_cutover_events` before returning success. The
table is append-only — no delete command, no update path — and the `actor` comes
from the server-side execution principal, never from the request body. The
gate rows (`drain_completed`, `cutover_authorized`) additionally carry the
human's attested `signed_by` in `detail`, so an auditor reads both what the
server knew and what the human declared. Read the whole trail with
`aq playbook cutover-gate-status --json` (current attempt) or straight from the
table (every attempt).

It outlives the commands: the deletion commit removes the drain surface but
keeps this table, because the record of who switched the fleet and when is
worth more than the code that wrote it.

## A config note

An unset `v1_admission` on a fleet already running `v2_engine` reads as
`closed` — the truthful description, since nothing dispatches V1 there. Writing
`v1_admission: open` next to `v2_engine: true` explicitly is a configuration
error and the daemon will refuse to start.
