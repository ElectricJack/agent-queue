# Pools final acceptance

**Task:** `smart-orbit.6`

**Date:** 2026-09-02

**Outcome:** pass

This gate exercised pool operation against an isolated PostgreSQL-backed
daemon with real Claude Code and Codex tmux sessions. The candidate scaled to
the configured project and profile limits, completed actual pushed edits in
the isolated fixture repository, drained after grace, quarantined and
recovered after an induced launch failure, and preserved task-lifecycle push
dispatch.

## Environment

- Branch: `aq/smart-orbit.6`
- Final tested revision: `fb760125` (equal to `origin/main` when final
  verification began)
- E2E root: `/home/jkern/.agent-queue-e2e-smart-orbit-6`
- API: `http://127.0.0.1:8097`
- PostgreSQL database: `agent_queue_e2e_smart_orbit_6` on the development
  PostgreSQL service at port 5533
- Session provider: tmux, using the dedicated `aq-e2e-smart-orbit-6` socket
- Harnesses: Claude Code 2.1.258 and Codex CLI 0.151.0
- Pool bounds: `min_active=0`, `max_active=2`; project cap 4; scale-down
  grace 15 seconds

The database, API port, vault, workspaces, repositories, and tmux server were
isolated from the production daemon.

The branch was fast-forwarded from its original `9e42a501` base to current
`origin/main` before final verification. This brought in the `smart-orbit.5`
operator guide, the pool worktree-slot race fix from `21cf0e82`, and the
Tier-1 fixture alignment from `e041f295`. The daemon was restarted after the
first fast-forward at `ef622134`; fresh real Claude task `bold-delta` pushed
`0401d33`, and fresh real Codex task `azure-vault` pushed `0eb9a2f`. Both
completed with `work_outcome=shipped` through their pool sessions. The final
PostgreSQL and dashboard verification below was then rerun from `fb760125`.

## Live dual-harness epic

The fixture container `fresh-vault` had four children split evenly between
Claude and Codex profiles. With all four children READY, the daemon launched
two sessions for each pool: four concurrent tmux sessions at the project cap.
All children committed and pushed actual fixture-repository changes, and the
container settled to COMPLETED (4/4):

| Task | Harness | Pushed commit | Work |
| --- | --- | --- | --- |
| `fresh-vault.1` | Claude | `622ae0d` | Correct `add()` and its tests |
| `fresh-vault.2` | Claude | `f6ef423` | Document the `add` API |
| `fresh-vault.3` | Codex | `08e774f` | Add and test typed `multiply()` |
| `fresh-vault.4` | Codex | `06a4773` | Document the package layout |

After work settled, both pools moved from desired 2 to desired 0 and had no
instances after the 15-second grace. No READY task remained awaiting a pool.

## Quarantine and recovery

A dedicated `worker-recovery` pool and READY task (`vivid-pinnacle`) isolated
the launch-failure test. The copied E2E Codex harness command was temporarily
changed to a nonexistent executable. The pool then reported:

- desired 1, ready work 1, zero instances;
- a populated `quarantined_until`; and
- `quarantined_reason` identifying a session death during startup and
  "process died before start".

After the harness command was restored, the quarantine expired and the daemon
launched a real Codex session (`f232b02e-1355-4cc9-bf62-54f5fc8badae`). It
claimed `vivid-pinnacle`, wrote the requested recovery marker, pushed commit
`f56e750`, closed successfully, drain-acked, and returned the pool to desired
0 with zero instances.

## Task-lifecycle rollback

The `worker` profile was changed from `lifecycle=pool` to `lifecycle=task`
through `aq pool set-lifecycle`. Creating `eager-crest` immediately produced
push-dispatched tmux session `s-eager-crest` with `lifecycle=task` and the task
assigned at launch rather than through a pool claim. The real Claude session
committed and pushed its requested marker, closed with
`work_outcome=shipped`, and stopped with `end_reason=task_closed`.

This proves the compatibility path can be restored through the supported
operator command and that the push scheduler still dispatches task-lifecycle
profiles.

## Health and dashboard

At the end of the live run:

- `aq task list --project e2e` returned zero active tasks;
- every remaining pool reported desired 0 and zero instances;
- doctor reported zero errors;
- `claims.holder_consistency`, every `hierarchy.*` check, and every `pools.*`
  check were `ok`; and
- the three doctor warnings were unrelated fixture-wide warnings for
  capability audit/legacy profiles and existing unschematized event types.

The live dashboard was started against the E2E daemon and rendered with
headless Chrome. Its agent rail and selected `worker-codex` pool window showed
the real idle session `p-worker-codex--e2e--d45142d9`, desired 1, idle 1,
"1 live instance", and bounds `[1–2]` while the API reported
`min_active=1`, `max_active=2`. The window's instance selector showed the same
session as running, unclaimed, and using `codex · gpt-5.6-terra`; after the
minimum was restored to zero, the session drained and both API and dashboard
returned to zero live instances.

The dashboard pool surface also passed 26 focused Vitest tests in
`Pools.test.tsx` and `useEventStream.wire.test.tsx`. Those tests cover
instance joining and selection, empty and quarantined states, live supply,
pool-bound settings and validation, per-project worker settings, and event
stream invalidation. Dashboard TypeScript typechecking also passed.

## Automated verification

The final pool/claim area sweep ran through `aq test` with
`POSTGRES_TEST_DSN` set and `AQ_REQUIRE_POSTGRES_TESTS=1`, preventing silent
PostgreSQL skips:

```text
402 passed, 0 skipped
```

The selection covered claim queries and commands, sizing, reconciliation and
carve-outs, full lifecycle integration, pool doctor checks, session
reconciliation and specification, worktree-slot starvation, and formula
resolution. PostgreSQL arms included the exactly-once claim race, routed
outer-join locking, end-to-end pull loop, startup quarantine, and
expired-quarantine relaunch. The new worktree regression specifically
verified that the dispatch which grows a slot pool acquires the slot it
created.

The stock Tier-1 kit was also run unmodified from `fb760125` in a second fresh
root and PostgreSQL database. All seven scenarios passed:

```text
PASS S1 pool sizing
PASS S2 claim loop as a worker
PASS S3 worker-filed work
PASS S4 formulas
PASS S5 fence + scope
PASS S6 doctor
PASS S7 PostgreSQL claim race
7/7 scenarios passed
```

## Resolved E2E finding

The first stock Tier-1 run exposed fixture drift from the current assignment
routing and fresh-context contracts. It passed S1 and S6 but reported
`no_ready_work` in five scenarios. The issue was filed with a
`discovered-from` edge as `smart-orbit.6.1`; another worker corrected the
fixture, and that child merged as `e041f295` while this gate was still active.
The clean 7/7 run above verifies the merged remediation, so no residual E2E
gap remains for this gate.
