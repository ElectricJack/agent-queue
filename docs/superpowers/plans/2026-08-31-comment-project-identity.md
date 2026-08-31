# Comment project identity repair

**Goal:** Restore comments and description edits for active tasks whose IDs collide with old archived tasks in other projects, without disclosing or deleting another project's comments.
**Architecture:** Add nullable internal project_id to task_comments. Backfill only when the active/archive identity resolves to one project; preserve unresolvable rows as NULL and never display them. New writes capture the locked active task project, reads filter by that project and revalidate identity in SQL, permanent cleanup targets the same project. Public comment response fields stay unchanged.
**Scope:** User-reported comments 422 on six legacy collisions. Existing worker claim, scope, CAS, archive restoration, and pagination contracts remain intact. Do not change task IDs, statuses, pauses, or recovery counters. Do not modify or remove historical tasks.

- [x] Reproduce HTTP422 and baseline existing real-DB tests in the already isolated task-findings-comments worktree.
- [x] Replace the fail-closed collision contract with regression tests proving local/authorized reads and writes, description CAS, archive comment isolation, scope rejection, and project-specific cleanup. Add migration backfill cases including ambiguous and orphan history; run RED.
- [x] Add migration f1d7a9c20b64 after e8b39a10c572 and nullable indexed project_id in tables.py. Backfill one-project identities only. Remove obsolete archive-collision denial from command and SQL write fences; preserve live project and claim predicates. Insert project from the locked active row and filter reads at SQL boundary. Keep project_id internal to DB responses.
- [x] Scope task, archive, and project permanent comment cleanup to project ownership. Keep same-project restoration behavior.
- [x] Run comment/HTTP/migration/project-move regressions with scrubbed live env and unique tmux temp paths. Final121 tests pass including SQLite/PostgreSQL parity and populated migration tests; use disposable PostgreSQL only. Lint, syntax, diff checks and independent security/lifecycle review pass.
- [ ] Commit/integrate/push main, back up production DB, restart only daemon preserving worker tmux processes, verify all six affected comment reads and browser controls; no test comments on live tasks. Record deployment.

## Review adjustments
The reviewer identified two lifecycle hazards. Project reassignment now serializes with comment writes and transfers only known source-project comments; moves that would merge a source/destination archive identity are refused. Archiving a colliding active task now raises archive_identity_conflict before archive timestamp changes or deletion, preserving both identities. Added RED/GREEN regression coverage for these paths and for unknown legacy comments remaining hidden after a collision is removed. The second review found no remaining blockers.
