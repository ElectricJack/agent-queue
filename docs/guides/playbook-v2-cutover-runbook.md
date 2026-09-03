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

All seven work while `playbooks.enabled=false`. That is deliberate: a fleet
that paused the subsystem with runs still `running` is exactly the one that
needs draining. All seven are operator-only and take a mandatory `--reason` of
at least 10 characters, stored verbatim in an append-only audit table.

| Command | What it does |
|---|---|
| `aq playbook v1-drain-status` | Every non-terminal V1 run, classified `live` or `orphaned`, with the options for each |
| `aq playbook v1-admission-close --reason TEXT` | Refuse new V1 runs. Paused runs stay resumable |
| `aq playbook v1-admission-open --reason TEXT` | The inverse. Refused while the fleet is on V2 |
| `aq playbook v1-run-cancel --run-id ID --reason TEXT` | Cancel one run, live or orphaned |
| `aq playbook cutover-switch --to v1\|v2 --reason TEXT` | Move the fleet between runtimes |
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

### 4. Switch

```bash
aq playbook cutover-switch --to v2 --reason "cutting over after a clean drain"
```

Refused unless the drain completed. Equivalent to setting
`playbooks.v2_engine: true`, plus an audit row recording who did it and why.

## Rollback

```bash
aq playbook cutover-switch --to v1 --reason "<what went wrong>"
```

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
from the server-side execution principal, never from the request body.

It outlives the commands: the deletion commit removes the drain surface but
keeps this table, because the record of who switched the fleet and when is
worth more than the code that wrote it.

## A config note

An unset `v1_admission` on a fleet already running `v2_engine` reads as
`closed` — the truthful description, since nothing dispatches V1 there. Writing
`v1_admission: open` next to `v2_engine: true` explicitly is a configuration
error and the daemon will refuse to start.
