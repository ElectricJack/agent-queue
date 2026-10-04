# Integration reconciler rollout and rollback

Use this guide for the incremental rollout approved in
[rev-agile-ridge revision 2](../superpowers/specs/2026-10-02-integration-train-simplification-review.md).
The [aggregate acceptance record](../reports/integration-aggregate-2026-10-04/report.md)
separates implementation evidence from the remaining production gates.
The configured integration owner publishes; workers supply pushed checkpoints.

## Required evidence before cutover

The operator owns live migrations, configuration, restarts and engine transfers.
Before transferring a repository or parent, retain these records together:

1. The deployed source SHA, additive schema head (`aq db current`), backup and
   reviewed/imported policy artifact SHA. Existing subjects retain their pins.
2. A recorded shadow week and reviewed comparison with the legacy decisions.
   An elapsed week, requested window or empty report is insufficient. The
   planned observation window is 2026-10-03 07:20 UTC through 2026-10-10 07:20
   UTC; it is not evidence that shadow was enabled or covered that window.
3. Real Git/disposable PostgreSQL scenarios for the selected boundary, covering
   conflicts, red/green exact-head checks, interruption, ownership and rollback.
4. Explicit human cutover approval identifying the repository, subject kind,
   exact artifact and evidence. Parent adoption also requires the root cutover
   receipt. Development adoption follows the parent gates.
5. The exact complete subject/version map from a current transfer preview, with
   no unresolved remote write. A changed version requires a fresh preview.

The [parent evidence](../reports/parent-rollout-2026-10-03/report.md) and
[development evidence](../reports/development-rollout-2026-10-03/report.md)
are disposable scenarios, not live adoption receipts. Keep the existing
`keen-stone-14` recovery and its preserved work; do not create a replacement.

## Automatic recovery and human holds

The reconciler visits durable due subjects, reads facts, evaluates the pinned
decision table, calls one primitive and persists the next schedule. Events
accelerate a visit; progress does not depend on replaying an event by hand.
Unknown observations and transient refusals have bounded waits. An indefinite
stop requires an explicit human gate.

| Observation | Next action under the reviewed policy |
| --- | --- |
| Busy publisher, capacity wait or transient refusal | Wait/back off with a due time; retain the same authority and work. |
| Unclaimed repair task | Keep its ordinal and wait for capacity rather than spending code attempts. |
| Stopped writer | Prove it stopped, preserve unpublished work and release its fenced lease. |
| Merge conflict or conclusive red on the current head | File/reuse the policy's writer; count only conclusive exact-head attempts. |
| Moved base | Rebuild, then request checks for the new exact candidate. |
| Interrupted publish | Reconcile the original journaled intent by exact remote read-back. |
| Failed parent child, no progress or exhausted budget | Follow the pinned table's explicit gate/continuation choice. |
| Reviewer rejection, product hold or open human gate | Preserve the hold until its authorized resolution. |

These are policy paths exercised by tests, not a claim that every production
subject already uses the reconciler. Legacy subjects retain the existing
automatic recovery and gated legacy controls until transferred.

Inspect with `aq integration status PROJECT_ID` and
`aq integration explain SUBJECT_ID`. The latter shows the recorded decision,
artifact and reason. `flush` makes subjects due; it does not approve a gate.
`integration.subjects_overdue`, `integration.subjects_held` and
`integration.trust` are the consolidated doctor checks. Retained legacy checks
still report legacy state while their removal gates remain open.

## Editing a project's policy

Copy the appropriate reviewed project root/parent table, or render a disabled
development source with `render_development_policy(project_id, settings)` in
`src/integration/development_policy.py`. Preserve its project scope. Edit the
declarative cases, primitive arguments, complete outcome schedules, waits,
budgets and gate choices; use the existing V2 proposal/review/import workflow.
The compiler rejects unknown outcomes, unbounded waits and incomplete gates.

Activate the reviewed policy through the existing project activation workflow.
`aq integration policy activate PROJECT_ID --mode MODE --policy FILE
--expected-generation N --reason REASON` updates the project's mode/settings under its current
generation; it does not compile/import a decision table or repin a running
subject. The imported artifact and activation must agree. New subjects use the
new pin; in-flight subjects continue using their immutable artifact. The
non-development `--policy` settings write still requires a disabled, drained
project in this snapshot; do not interpret artifact pinning as permission to
bypass that refusal. The proposed end-to-end policy-edit-without-drain surface
is not fully realized yet.

Root and parent subjects preserve trusted exact-SHA green before default-branch
publication. Development's `advisory` or `none` validation settings also cannot
bypass that publisher: without trusted exact-head green they reach the reviewed
evidence gate. An answer alone cannot manufacture green evidence. This is the
[documented conservative difference from legacy development](../superpowers/specs/2026-10-02-development-subject-adapter.md#validation-and-the-exact-green-invariant).

## Exclusive engine transfer

Use a deployed checkout containing the consolidated CLI. An older installed
`aq` can show flat legacy commands even when this source tree has the new
surface; upgrade/restart through the operator workflow before using new flags.
The supported legacy group retains the transfer command:

```bash
aq integration legacy engine-transfer REPOSITORY_ID --engine reconciler --json
```

For a parent, add `--parent-task-id PARENT_ID`. Development transfer currently
exists only as the tested `transfer_development_engine` mechanism in
`src/integration/development_runtime.py`; `development_engine_transfer` is its
journal label, not a registered command. The CLI/CommandHandler transfer path
selects root or parent ownership and does not expose that development mechanism.
Development cutover/rollback requires this command wiring to be delivered
first. Do not call the Python helper directly to bypass CommandHandler.
Apply the fresh complete map, repeating the version option for every subject:

```bash
aq integration legacy engine-transfer REPOSITORY_ID --engine reconciler \
  --apply --expected-subject SUBJECT_ID:VERSION --reason 'approved cutover' \
  --evidence SHADOW_REPORT --evidence SCENARIO_RECORD --evidence HUMAN_APPROVAL \
  --json
```

Enable the active loop only under the approved rollout configuration. Retain
the accepted transfer response and journal entry, input/result versions,
artifact and repository identity; verify subsequent visits progress or retain
their named gate. One publisher per repository, journal-before-push, expected
old heads, writer fences and trusted exact-green checks remain binding.
Resolve an uncertain push under its original owner/intent before transferring
either direction.

## Migration and deletion

Workers never migrate the operator database. The operator backs up, deploys
the additive revisions and uses `aq db upgrade`; record `aq db current` and
health afterward. Revision `a00000000064` adds the retirement archive. It does
not drop legacy tables and remains at its reviewed revision identity.
At final integration, the delivery owner must check the destination's Alembic
head and re-chain a colliding sibling revision before publishing. Do not
renumber this worker's revision in advance of that destination.

Deletion is a separate gate after cutover. All relevant subjects must have
transferred, the legacy-control/doctor removal conditions must be proven, and
each family's last source/schema reference must be removed. Preview with
`aq db retire`, which also reports row counts and outside foreign keys.
No family in the aggregate acceptance snapshot is ready for deletion.

Follow [table retirement](migrations.md#retiring-legacy-integration-tables) for
the fresh custom-format backup, family grouping and guarded `--apply`. The
transaction locks, archives every row, verifies row counts/digests and records
receipts before dropping tables. Keep the archive and backup. A downgrade past
revision 64 refuses once retirement receipts exist. `restore-retired` restores
rows and columns; restore the backup's indexes, defaults and constraints before
running legacy code that requires them.

## Rollback

Before deletion, preview the same repository/parent with `--engine legacy`.
Apply its fresh complete version map with a reason. An unresolved publication
refuses rollback too. Retain the subject, original episode/operation, artifact,
receipts, writer ordinal/fence and binding human gates; verify legacy resumes
only after the accepted audited transfer.

Feature-off alone stops visits but does not return ownership to legacy. For a
full rollback, transfer every affected subject first, disable active/shadow
flags as approved, restore the legacy event rules and use
`aq restart --no-dashboard` so worker sessions survive. Leave active visits on
when other subjects still belong to the reconciler. Explicit holds and verified
human answers survive transfer and restart.

After deletion, restoring a runnable legacy schema/code is required before
transferring ownership back. Use the retained archive/backup and the operator's
restoration procedure; a flag change or empty recreated table is insufficient.

## Final aggregate acceptance

After collection and the Jack-owned deletion gate, the P4 integration owner
files its aggregate verifier on the exact epic head and observes the parent's
required CI. Parent review and delivery then bind that verified head; the
supervisor supplies its exact-head authorization for the untyped P4 root.
Run the configured full hosted CI once on the final candidate. Retain its SHA,
workflow/run URL, required job conclusions,
trusted producer binding and delivery receipt. A child test run or a green
older candidate is insufficient. Re-run the inventory on the final tree and
reconcile all remaining numeric gaps before declaring phase 4 complete.
