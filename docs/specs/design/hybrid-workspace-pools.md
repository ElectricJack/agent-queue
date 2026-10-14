# Hybrid workspace pools

Task: `fleet-vault-71`. Status: implementation specification.

A project may keep its default repository on a fast filesystem and a bounded
second repository kind on a filesystem required by a native toolchain. This
adds no project-specific paths, routing overrides, or model choices.

## Requirements and placement

Use the existing `requires_kinds` rows and `aq task create --requires-kind`.
An ordinary pool task requires `project-repo`. An explicit single non-vault
kind replaces it; `vault` remains an optional attachment. A vault-only legacy
requirement retains the default repository. Multiple repository requirements,
duplicate repository positions, unknown kinds, and non-writable/non-lockable/
non-Git repository kinds fail closed for pools. Multi-repository task-session
acquisition remains separate. Labels may document native work, but a label alone
does not change placement: the owner must file the explicit requirement.

Demand, capacity, placement, launch, and claiming use the same primary kind.
A session may claim only work matching its actual registered, enabled workspace
at its work directory. It cannot request a different kind to bypass that proof.
Pool profile limits remain global, and project limits remain shared across
kinds. Warm floors use only the default kind. Idle capacity on another kind
does not satisfy demand; ordinary bounded rebalance drains surplus idle workers
after the existing grace and launches replacements without increasing caps.

Workspace-kind frontmatter gains optional positive `max_slots`. The effective
worktree slot limit is the minimum of this value and the project limit. Omission preserves
the existing limit. Growth, acquisition, availability, and retirement agree on
this bound. An operator can set a special kind to two slots. Locked slots above
a reduced limit remain intact until their owners finish.

## Bases and cutover

Default repository lookup selects only enabled non-slot `project-repo` rows;
special bases are resolved by kind. Legacy NULL kinds mean `project-repo`.
Existing source-type preference remains within that set. Registration never
relocates a base with linked slots and never rewrites its Git common directory.

An operator cutover previews the old and new registered base identities, kind,
paths, locks, claims, source state and integration ownership. Apply requires the
exact preview proof and revalidates it. It disables the old base and its slots
for new work and enables the new base. Existing holders retain their exact paths,
locks, branches, checkpoints and claim epochs. An idle worker on a disabled
workspace drains before claiming again. Cutover does not remove directories,
Git refs, workspace records, ownership rows, or checkpoints. Retirement is
deferred while any writer/claim/pin is present or its provenance is unknown;
retained source is reported for the operator to reconcile separately.

There is no in-place kind reassignment. The two native slots are provisioned on
a distinct special base; no active default slot is converted in place. This avoids repurposing an
old writer while its task still has source to publish.

## Validation and rollout

Regressions cover default/vault compatibility, custom/unknown/multiple kinds,
targeted and next claims, mixed-kind pool placement and rebalance at shared caps,
per-kind bounds, enabled default-base selection, stale preview refusal, and
cutover with active holders preserving all source. Run focused and related
`aq test` checks and the real disposable swarm smoke test. Do not migrate the
operator database from a worker slot.

The owning operator performs the live project cutover after delivery and schema
upgrade. Its receipt must prove the default base/new slots, exactly two native
slots, explicit requirements and incompatible-claim exclusion, retained old
writers until completion, and updated project instructions. Code tests alone do
not close the original migration task successfully.

The operator registers the target using `aq project add-workspace --no-enabled`
(or the equivalent `add_workspace` command), then previews `workspace_cutover`
with `project_id`, `source_id`, and `target_id`. Applying uses the returned
`proof` as `expected_proof` with `apply: true`. The receipt is an admission switch,
not a destructive-retirement authorization. Old sources remain registered and
on disk even after writers finish. Existing guarded `workspace_reap` may later
remove out-of-cap slots only with fresh generation, claim, session, pin, sentinel
and Git-common-directory checks; it never follows a cutover receipt blindly.

## Operator handoff v1 (fleet-vault-71)

The delivery receipt on the AQ task supplies the full commit SHA containing this
document. It preserves wise-stone-87 ancestry through
`116134665b82d7d2f4551f9d3e91dd14ce2c8555`. A pushed branch or open PR is not a
loaded daemon revision. Before acting, the operator records the delivered SHA,
the actual loaded daemon/CLI revision, and schema revision `a00000000096` (or a
later integrated descendant). Only the operator upgrades the live database.
The worker's installed CLI did not expose `workspace-cutover` during development;
the checkout's generated CLI did. Do not interpret missing controls as permission
to edit database rows directly.

Prerequisites:

1. Use the same delivered revision for CLI and daemon. Confirm
   `aq project workspace-cutover --help` exposes `--expected-proof`,
   `aq project add-workspace --help` exposes `--no-enabled`, and
   `aq db current` reports the integrated schema. Worktree execution must be
   enabled. Existing overrides remain authoritative; no production pool cap,
   requirement, project path, or profile is changed by installing this feature.
2. Verify the intended independent fast clone and native clone are clean,
   identify their actual origins/default branch/HEAD/rescue refs, and inventory
   current old-base workspaces, sessions, claims, integration owners and pending
   checkpoints. The target and source origins must identify the same repository.
   Cutover observations are read-only; they do not transport commits or refs.
3. The project owner authors a project-scoped kind definition under the operator
   vault's `projects/<project-id>/workspace-kinds/windows-native.md`:

   ```yaml
   ---
   id: windows-native
   description: Repository for tasks requiring the native Windows toolchain
   writable: true
   lockable: true
   is_git_repo: true
   default_lock_mode: exclusive
   auto_attach: false
   mode: worktree
   max_slots: 2
   ---
   ```

   Let the existing workspace-kind watcher reconcile it; read back
   `aq project list-workspace-kinds --project-id <project-id>` to verify
   `mode: worktree` and `max_slots: 2`. Keep the shared project cap at least two
   if two native slots are needed. Default slots use their existing cap;
   the special limit never increases the shared project/profile/fleet caps.

Commands for the owning operator, substituting verified paths and returned IDs:

```bash
aq project add-workspace --project-id <project-id> --source link \
  --path <fast-clone> --kind-id project-repo --no-enabled
aq project workspace-cutover --project-id <project-id> \
  --source-id <old-base-id> --target-id <new-disabled-base-id> --json
aq project workspace-cutover --project-id <project-id> \
  --source-id <old-base-id> --target-id <new-disabled-base-id> \
  --apply --expected-proof <exact-preview-proof> --json
aq project add-workspace --project-id <project-id> --source link \
  --path <independent-native-clone> --kind-id windows-native
```

Save the complete preview/apply receipts. Any changed source identity, HEAD,
dirty paths, claim file, workspace generation/lock/pin, or recorded owner/session
changes the proof and requires another preview. An enabled or dirty target,
target holder, shared Git common directory, or ambiguous enabled source family
is refused. This switch only changes admission. It leaves active old writers
and their source paths intact and drains their sessions on the next idle claim.

File native work with `aq task create --project <project-id> ... --requires-kind
windows-native`. A label alone is insufficient. Ordinary work omits the
requirement; do not require both repository kinds on a pool task. Existing work
must be reconciled by its owner through supported task workflows; this feature
does not reclassify or move held tasks. Slot creation remains lazy: two slots
are the native upper bound, and two concurrent admitted native tasks are needed
to demonstrate both slots. No special idle warm pool is created.

The ME owner's receipt (owner task reported as `azure-journey-94`) must identify
the loaded SHA and show new ordinary claims under the fast base, two native
slots under the verified mounted filesystem, native requirements and rejection
of claims from an incompatible registered workspace. It must also contain the
ME `CLAUDE.md` guidance commit and completion/publication receipts for every
retained old writer. The initial task reports the proposed fast path and timing;
those statements are context, not independently verified live acceptance here.

Deferred retirement is explicit. Disabled old families remain on disk and in
the registry after task completion. The reaper refuses disabled-base families;
the cutover receipt never authorizes their deletion, re-enabling, kind changes,
or blanket lock release. The owning operator must separately reconcile exact
current generations, claims, live processes, pins, ownership, checkpoints and
published refs before any final removal through a supported retirement path.
This delivery does not supply automated deletion of the retained cutover family.
Until that receipt and the other live acceptance receipts exist, the original
migration task is partial/blocked, even if generic implementation tests pass.
