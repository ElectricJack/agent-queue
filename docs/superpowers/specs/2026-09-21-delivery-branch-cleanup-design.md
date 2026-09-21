# Delivery branch cleanup and the undelivered-work archive hold

Task `quick-ridge`, 2026-09-21.

## Problem

Development delivery never deleted a ref. Every worker task pushes
`aq/<task-id>`; every batch pushes a candidate snapshot
`aq/development/<sha256(project)[:12]>/<head>` and, for children, a parent
assembly `aq/development/parent/<sha256(parent)[:16]>/<assembly>`; every repair
is a task on `aq/development-repair-<id>` (a failed close can add a `-wip`
sibling). The publisher lands merges, so the refs look unmerged on GitHub until
somebody deletes them. On 2026-09-21 origin carried 961 branches, 893 of them
already merged.

Separately, the hourly auto-archive took two COMPLETED tasks whose work never
landed: `nimble-nexus` (archived 09-20 while its batch was parked — already
covered on main by `_development_integration_hold`, 65484190e) and
`fleet-meadow` (its `repo_id` named another project's repository, so the
publisher never collected it and `_development_delivery_pending` said "not
pending").

## Decisions

1. **One question, asked by both deleters.**
   `live_branch_references(conn)` (`src/integration/delivery_branches.py`)
   returns every branch something still needs, with the first reason found.
   It is fleet-wide and by name (task ids are unique). It holds: a task that
   can still run or has a live session (plus `-wip`); a COMPLETED task whose
   delivery is pending, foreign repository ids included; members, targets and
   carrying assemblies of unsettled journal rows (`prepared`, `publishing`,
   `parked`); sources of an open development repair; every
   `integration_branch_owners` row not `released`; tasks of live legacy repair
   operations; active legacy batches and their member refs; unsettled
   promotion intents; live `task_branch_origins` in hierarchy/train projects;
   pending branch discards. Over-holding keeps a branch; under-holding loses
   work, so doubtful rows hold.

2. **A landed delivery arms its own cleanup.** A default-branch journal row
   gets `evidence.branch_cleanup = {"state": "pending"}` on every path that
   lands it: `publish` from the sweep, `reconcile` confirming an ambiguous
   push, `reconcile_parked` resolving a parked batch, and operator `adopt`.
   Rows journaled before this change carry no marker and are left to doctor,
   so deploying it does not trigger a bulk delete.

3. **What a landed row deletes** (`collect_delivered_branches`, run from
   `tick` after the sweep, isolated from it, under the publisher exclusion,
   after a fresh `fetch --prune`):
   * each COMPLETED member's branch, when the remote head is the delivered
     `source_sha` or is on the default branch; its `-wip` sibling only when
     on the default branch;
   * each assembly ref carrying one of the row's members, once every member
     it carries is on a delivered/adopted default-branch row and all its
     journal rows are `delivered` — this also removes a superseded candidate
     of a batch that parked;
   * for a parked batch resolved by main ancestry, the branch of its repair
     when that repair ended FAILED/BLOCKED (the repair is moot). The source
     worker's branch goes only as a delivered member, never because its batch
     was abandoned.
   Only `aq/` branches, never the default branch, never a held branch. Each
   delete is `push --force-with-lease=<ref>:<observed head>`; the remote is
   then listed and every branch classified `deleted` (gone, including already
   gone), `moved` (kept) or `failed` (retried).

4. **Record and retry.** The row's `branch_cleanup` holds `deleted`
   (branch, sha, kind), `kept` (branch, reason), `missing`, `attempts`,
   `last_error`, `next_attempt_at`, `state`. Unconfirmed deletes retry with
   backoff from 5 minutes to 6 hours, up to 8 attempts (`exhausted`). A
   `development.branches_deleted` event is logged per run that deletes.

5. **The backlog is doctor's.** `aq doctor --check integration.landed_branches`
   scans each development project's origin (the publisher's clone) for `aq/`
   branches whose work is on the default branch and that nothing holds;
   `--fix` deletes them on the same lease. "On main" is: head is an ancestor;
   or every non-merge commit beyond main has a twin on main with the same
   author e-mail, author time and subject (the task asked for "by commit
   subject"; the author stamp stops a generic subject from matching different
   work), and every merge beyond main is *clean* — its tree equals
   `git merge-tree --write-tree` of its parents — so a hand-resolved merge
   keeps the branch. Branches outside `aq/` are counted, never touched.

6. **Archive sweeps hold undelivered work.** `archive_task(...,
   hold_undelivered=True)` — used by auto-archive, `archive_completed_tasks`
   and bulk `archive_task` by project — refuses with
   `hierarchy.delivery_pending` when the subtree holds a COMPLETED task for
   which `_development_delivery_pending(tasks, include_foreign_repos=True)` is
   true. The new flag counts a `repo_id` that is not one of the task's
   project's repositories (fleet-meadow); readiness keeps the old meaning.
   A single explicit archive stays the operator's call.

## Not done here

* Legacy hierarchy/train refs (`refs/aq/integration-*`,
  `aq/integration-repairs/*`) belong to `src/integration/cleanup.py`.
  `cancel_preserving` keeps its owner refs as quarantine by design; those
  owners hold their branches here too.
* Open pull requests are not consulted. Deleting a PR's head branch closes
  the PR on GitHub; development mode does not use PRs, and every deleted
  branch's work is already on main.

## Evidence (read-only dry run, 2026-09-21)

Against the live origin and holds computed from the operator database:
927 `aq/` branches; 681 landed and unreferenced (660 by ancestry, 21 by
author stamp and subject, every merge clean), 237 landed but held (181 by
hierarchy-era owner rows still `reserved`, 20 assemblies of parked batches,
18 parked-batch members, 8 attached owners, 6 active legacy batch refs, 3
undelivered COMPLETED tasks, 1 `handoff_pending` owner), 7 not landed, 46
non-`aq/` branches out of scope. The holds query took 0.3 s, the scan 1.5 s.
