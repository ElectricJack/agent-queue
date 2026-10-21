---
tags: [guide, ops, waits]
---

# Verified wait and hibernation cutover

Use a daemon build containing every event-driven-waits prerequisite and the
cutover change. The approved implementation is
`2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md`
(§14 and §17) in the project vault. Shipped defaults enable verified waits,
hibernation and scheduler waits, and refuse version 1 registration bodies.
Existing explicit configuration remains authoritative. Run live migration,
configuration and installed-vault actions from the operator/global supervisor
context; worker slots must keep their database refusal sentinels intact.

## Readiness and migration

Record the source commit and all prerequisite deliveries. The implementation
plan's historical migration number is not an upgrade target: use the current
checkout's Alembic head. Verify the atomic `job submit --wait` writer uses verified
waits before enabling live defaults; a legacy writer creating new `agent_waits`
after the one-time bridge is a cutover blocker. Inspect explain/CI watcher
composition, capability grants, all installed harnesses and failure/restart checks.
Do not infer production readiness from unit tests alone.

Before updating an existing install, pin all three flags false explicitly so
new shipped defaults cannot activate work before the migration and canary:

```yaml
wait_policy:
  v2_enabled: false
  hibernation_enabled: false
  scheduler_waits_enabled: false
  v1_bodies: translate
```

Back up the operator database and configuration using the normal operational
procedure. From the operator context:

```bash
aq db current
aq db upgrade
aq doctor --check waits.schema
aq doctor --check waits.orphans
aq doctor --check profiles.system_drift
aq doctor --check skills.installed_drift
aq doctor --check harness.drift
```

Schema setup refuses a database behind code. Do not bypass the guard or start an
older binary against a database with parked continuations. Missing schema is an
operator migration action, never a doctor auto-fix.

Shipped vault files are write-if-absent. Changing source skills does not update an
installed copy. Reconcile each reported installed skill with the shipped version,
preserving reviewed operator customizations. Supervisor and worker-template
`## Capabilities` grants merge additively on startup/profile reload unless
`capability_sync: false`; non-template profiles can use:

```bash
aq agent profile-reseed --profile-id PROFILE_ID --grants-only
```

Re-run drift checks and review intentional differences. Never edit derived worker
rungs. Refresh installed harness hooks through their supported installer when
`harness.drift` requests it. Restart with `aq restart --no-dashboard` to preserve
live agent sessions; do not use plain stop/start for an update.

## Canary, adoption and expansion

Enable all three flags for one project under `wait_policy.projects.PROJECT_ID`,
keeping global flags false. Reload configuration using the deployment's supported
configuration path. Inspect effective project flags with `waits.schema`.

```bash
aq wait bridge --project PROJECT_ID --mode agent-waits
aq wait bridge --project PROJECT_ID --mode agent-waits --apply
aq wait explain --task TASK_ID --project PROJECT_ID --history
aq wait graph --project PROJECT_ID --costs
```

Preview first and resolve every held migration identity before proceeding. The
bridge is receipt-backed, restart-safe and preserves the original creation and
deadline instants, including already expired records. It adopts legacy agent waits,
handles legacy `WAITING_INPUT` ownership, and audits scheduler branch blockers.
Global activation never means dropping legacy data. Inspect bridge progress,
unchanged age/deadlines, outcomes, reservations and retained checkpoint heads.

Exercise an in-place outcome and hibernate/resume on each enabled harness. Verify
one writer, publication/detach before claim release, fresh prime, exact saved HEAD,
acknowledgement, held mail and no repeated monitoring turns. Exercise source
completion during stop, daemon restart, provider outage, failed publication,
missing summary and head drift. Check schema/orphans and measured costs. Expand
project overrides, then enable the three global flags only after evidence is
recorded. Set `v1_bodies: refuse` after installed callers send version 2; verify a
raw v1 registration returns `wait.upgrade_required` and `detail.example`.

## Rollback without losing work

Keep the new daemon and schema. First set `hibernation_enabled: false` globally
and in enabled project overrides. This stops new suspension decisions; transitions
already started must finish or remain safely held. Existing parked continuations
continue outcome dispatch, capacity admission, fresh launch and acknowledgement.

Then set `v2_enabled: false` and `scheduler_waits_enabled: false` globally and in
all project overrides, and set `v1_bodies: translate`. Reload configuration.
Explicit legacy clients can again register version 1 bodies; the default CLI
continues sending version 2, so a rollback caller uses `--schema-version 1`.
Existing verified waits and continuations continue draining regardless of flags.
Scheduler reconciliation retracts its waits and reservations. Observe
`waits.schema`, `waits.orphans` and `aq wait graph` until all nonterminal
continuations are acknowledged or explicitly dispositioned.

Never delete continuation rows, checkpoint summaries, saved heads or outcomes to
make counts fall. For orphan ownership, preview `aq doctor --check waits.orphans`,
then use `--fix` from the operator context. Fix preserves checkpoint evidence and
leaves live/locked ownership or missing summary/head evidence held for intervention.
A flag-only rollback does not permit an older binary or schema downgrade while
any task is `HIBERNATED`/`RESUME_PENDING` or any continuation remains nonterminal.

## Validation record

Record the source SHA, schema revision, effective flags, installed drift decisions,
bridge receipts/holds, harness evidence and rollback drain counts. Required code
checks include the task's migration/doctor/v1/inventory/command-surface files,
cutover and wait area tests, generated-artifact drift and the disposable swarm
smoke (`scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`). A passing isolated
smoke does not authorize a worker to manage the live daemon or operator database.
