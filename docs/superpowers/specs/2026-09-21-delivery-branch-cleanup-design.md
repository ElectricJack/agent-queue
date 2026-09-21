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

5. **Everything else is `git.stale_branches`** (`src/doctor/git_checks.py`,
   rules in `delivery_branches.find_stale_branches`; the supervisor's stall
   sweep runs it — the shipped supervisor profile says so and now carries the
   `doctor` grant). For each development project's origin, an `aq/` branch is
   stale by exactly one rule:
   * `landed` — head is an ancestor of main; or every non-merge commit beyond
     main has a twin on main with the same author e-mail, author time and
     subject (the brief's "by commit subject"; the stamp stops a generic
     subject matching different work) **and** every merge beyond main is
     clean — its tree equals `git merge-tree --write-tree` of its parents — so
     a hand-resolved merge keeps the branch;
   * `integration` — an `aq/integration/*` ref with ≥1 owner row, all
     `released`, and every operation tied to it (owner id, or any operation
     on the batch whose `integration_branch` it is) outside
     active/escalated/human_required. Never by ancestry; no owner recovery;
   * `expired` — the branch (and `-wip`) of a FAILED task, or one whose
     `work_outcome` is `abandoned` (metadata or latest completion record),
     live or archived, 14 days after `max(updated_at, latest completed_at)`.
   Held branches (decision 1) are reported, not deleted; `--fix` re-scans
   under the publisher exclusion and deletes the rest.

6. **Every deletion is restorable, and only `aq/` is ever touched**
   (`delivery_branches.delete_branches`, used by both deleters). It refuses
   outright anything outside `aq/`, the default branch, `main` and
   `gh-pages`. Before pushing it writes every tip main cannot reach into a new
   thin bundle (`--not <main head>`, verified with `bundle verify` and
   `list-heads`) at `<data_dir>/backups/branch-deletions/<yyyy-mm>/<utc>-<repo>.bundle`
   — `git bundle` cannot append, so each run adds a file rather than
   rewriting a monthly one — and appends every branch to
   `<data_dir>/backups/branch-deletions/<yyyy-mm>.tsv` as
   `branch, sha, reason, bundle|-, recorded_at, repository` (fsynced; the
   first two columns match the supervisor's `deleted-branches-2026-09-21.tsv`).
   Restore: `git bundle unbundle <bundle>` then `git push origin
   <sha>:refs/heads/<branch>`.

7. **Archive sweeps hold undelivered work.** `archive_task(...,
   hold_undelivered=True)` — used by auto-archive, `archive_completed_tasks`
   and bulk `archive_task` by project — refuses with
   `hierarchy.delivery_pending` when the subtree holds a COMPLETED task for
   which `_development_delivery_pending(tasks, include_foreign_repos=True)` is
   true. The new flag counts a `repo_id` that is not one of the task's
   project's repositories (fleet-meadow); readiness keeps the old meaning.
   A single explicit archive stays the operator's call.

## Not done here

* Hidden legacy refs (`refs/aq/integration-*`, not branches) stay with
  `src/integration/cleanup.py`. `aq/integration-repairs/*` branches are
  ordinary `aq/` branches here: deleted only when landed and unreferenced.
* `BranchDiscardService` (`aq task delete --branches delete` in
  hierarchy/train projects) still deletes without a bundle; it acts on an
  explicit operator choice through the GitHub App transport and a different
  store. Filed as a follow-up so it gets the same backup.
* Open pull requests are not consulted. Deleting a PR's head branch closes
  the PR on GitHub; development mode does not use PRs, and every deleted
  branch is either on main or bundled.
* No new owner-recovery logic: stale `reserved` owner rows keep holding their
  branches (follow-up `quick-torrent`).

## Evidence (read-only dry runs, 2026-09-21)

Against the live origin and holds computed from the operator database:

* Before the supervisor's hand cleanup (973 refs): 681 landed and
  unreferenced (660 ancestry, 21 subject), 237 landed but held (181 by
  hierarchy-era owner rows still `reserved`), 7 not landed, 46 outside `aq/`.
* After it (380 refs), full policy: 202 stale (191 ancestry, 11 subject),
  123 held (108 `reserved` owners, 7 `attached`, 3 undelivered COMPLETED
  tasks, 2 active legacy batches, 2 parked batches, 1 `handoff_pending`),
  7 kept, 46 outside `aq/`. Rule (a) matches nothing yet (every
  `aq/integration/*` owner is `reserved`/`attached`); rule (b)'s four expired
  branches are already gone or held. Holds 0.3 s, scan 0.4–1.5 s.
