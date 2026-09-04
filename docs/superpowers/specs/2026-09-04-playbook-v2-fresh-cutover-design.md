# Playbook V2 fresh-cutover design

## Goal

Move the live Agent Queue installation to the typed Playbook V2 runtime, replace
the old V1 playbook definitions, and remove completed task history only after
the V2 runtime passes a controlled smoke test.

## Decisions

- The replacement set is `default-assignment-routing`, `default-pipeline`,
  `memory-consolidation`, and project-scoped `pr-merge-sweep`.
- The disabled `coding-reflection` playbook is retired without a replacement.
- The orchestrator remains paused throughout staging, drain, activation,
  switching, restart, and validation.
- Reviewed V2 artifacts are imported through `playbook_v2_import`; no database
  row or artifact file is inserted by hand.
- The old V1 vault definitions are removed from the live registry at the
  controlled switch. Dormant V1 engine code remains available only for the
  rollback window required by the existing cutover protocol, then is deleted
  under the Package 7 cleanup plan.
- Completed-task cleanup is destructive and runs last. It removes rows whose
  status is exactly `COMPLETED` from both active and archived task storage. It
  does not remove READY, DEFINED, IN_PROGRESS, PAUSED, BLOCKED, or FAILED work,
  and it does not remove projects, profiles, V2 artifacts, activations, cutover
  audit events, or playbook receipts.

## Replacement behavior

### Assignment routing

The V2 router handles `assignment.route.requested` with one bounded,
tool-disabled `playbook-compiler` LLM step. It selects the least expensive
compatible intelligence class and never changes provider because of temporary
worker occupancy.

### Default pipeline

The V2 default pipeline owns five deterministic rules: per-task review,
per-branch final review, spec ingest, proposal approval gate, and approved
proposal commit. Review creation uses structural `review_task` and dedup-key
guards, eliminating review-of-review recursion. All command effects retain
idempotent keys and explicit failure terminals.

### Memory consolidation

The V2 consolidation playbook handles the 24-hour timer through one bounded
`supervisor` LLM tool loop. It selects active projects by churn, renders one
prompt per target, and creates one consolidation task per target without
performing vault mutations itself.

### PR merge sweep

The V2 sweep is authorized only for `project:agent-queue`. Every 30-minute
event ensures one deduplicated `pr-merger` task and routes it to
`deep-medium`. Its reviewed artifact hash is
`sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525`.
The bundle remains non-importable until a human operator records approval in
its `review.md` frontmatter.

## Deployment sequence

1. Verify `main`, targeted tests, the dashboard build, and daemon health.
2. Copy the four complete reviewed bundles beneath the configured vault root.
3. Record the operator's review of the PR sweep without altering artifact
   bytes, its digest, source digest, or contract fingerprint.
4. Import all four bundles inactive and list them back by full hash.
5. Close V1 admission and cancel every orphaned V1 run with an audited reason.
6. Activate all four V2 artifacts and require `ready` health for each.
7. Replace live playbook Markdown with the reviewed prose-only V2 sources and
   remove the disabled `coding-reflection` source if present.
8. Obtain the existing G1 drain sign-off and two-person G2 authorization, then
   switch `playbooks.v2_engine` to true.
9. Restart the daemon so the landed importer and updater fix are loaded while
   keeping scheduling paused, then verify health, the dashboard, activation
   health, graph projection, and one controlled V2 rehearsal.
10. Purge completed tasks only after the smoke checks pass, verify both active
    and archived completed counts are zero, then resume scheduling.
11. After the existing rollback window closes, execute the Package 7 removal
    plan to delete the dormant V1 runtime and compatibility commands.

## Human gates

The implementation must not invent operator identities. The PR sweep review,
G1 sign-off, and both G2 roles require user-supplied human names. G2 continues
to require two distinct people because the live command enforces that rule.
The cutover pauses at those gates if the required attestations are absent.

## Completed-task purge safety

Before deletion, write a JSON manifest containing every selected task id,
project id, title, status, storage table, and hierarchy parent. The manifest is
stored outside the live database beneath the Agent Queue data directory. Count
the selected rows twice: once in the manifest and once immediately before the
write. Abort on a mismatch.

Active completed tasks are deleted deepest-first through the normal task
deletion path so related dependency, result, metadata, layout, and gate state
uses existing cleanup semantics. If a completed task owns any non-completed
descendant, abort rather than cascading into work outside the authorized
status. Archived rows with status `COMPLETED` are removed through
`delete_archived_task`, which also deletes their completion record and task
comments. The purge records the manifest path and final counts in the cutover
operator log.

## Failure and rollback behavior

- An import failure leaves no activation and no partially referenced artifact.
- An activation with non-ready health blocks G1.
- A V1 drain that is not zero blocks the switch.
- A failed V2 smoke test leaves the orchestrator paused and invokes the normal
  switch-to-V1 rollback while the rollback window remains open.
- A completed-task purge failure stops immediately; already deleted completed
  tasks are recoverable only from the pre-cutover database backup, so the
  backup is verified before the first deletion.
- Task deletion never runs before the V2 smoke test passes.
