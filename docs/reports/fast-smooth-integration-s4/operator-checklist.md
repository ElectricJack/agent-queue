# Operator cutover and rollback checklist

These are proposed handoffs under approved `rev-fair-lantern` revision 2, §§11–12
(SHA256 `4518d9296649ddc2f0192ff73dd682864fb938e3d9687069282b4f6b0a86453f`). Every action
below belongs to an authorized operator in the named project. Complete the
corresponding JSON manifest with full OIDs, digests, evidence and named owners
before applying a live plan. `null` means unverified, not absent. A fixture OID
from this report is never a live source or backup.

## Agent Queue

1. Identify the actual repository IDs, clone/remote identities, protected and
   default/delivery refs, deployment binding, current code/schema version and
   project playbook/check-producer digests. Confirm that the approved Red batch
   semantic gap and inherited failing source checks in the report have been
   resolved or explicitly dispositioned by their owners on the exact proposed
   source. Unapproved revision-3 proposals are not activation prerequisites
   established by this validation task.
2. From an operator context, inspect `aq integration cutover-containment --help`
   and run its default read-only preview for that repository. Inventory every
   active claim/preparation/branch owner, batch, repair, source generation,
   source/base OID, private/provenance ref and uncertain remote result. Resolve
   the specification's historical example by full OID and fresh ancestry;
   abbreviated comments and completion status do not prove delivery.
3. Classify each generation as contained, retained pending, equivalent-only,
   missing/ambiguous, code-free, retired or an active/uncertain writer. Retain
   original objects before any switch. For equivalent-only history, obtain an
   explicit per-generation `delivered_by_other_means` disposition with carrying
   commit/equivalence/retention evidence, or choose a reviewed real merge and
   current required CI. Preserve intentional reverts. Disposition excludes an
   obligation; it never changes strict ancestry to contained. An unavailable
   original is an explicit owner blocker, not a fake receipt.
4. Observe strict ancestry in diagnostic shadow for a full scheduling/delivery
   cycle without dual publication authority. Export and verify a restorable
   PostgreSQL backup and full Git ref/OID manifest including private sources,
   candidates and local-only objects. Record policy/config/deploy bindings and
   recovery locations. The operator deploys additive schema; workers never
   migrate the live database.
5. Pause this repository's new admissions and integration writes through
   supported project controls for the protocol switch. Let operations finish
   or preserve dirty work, frozen inputs and source identities while revoking
   or expiring old write authority. Resolve uncertain pushes by fresh Git
   observation. Unknown remote outcomes and live legacy publishers block apply.
   Do not reset claimed slots, kill unrelated agents or recut active stacks.
6. Refresh the preview. Review the exact digest and writer/config/version fence;
   apply through CommandHandler only when every blocker is resolved. One strict
   reader contract and one publisher must take authority together. Preserve
   explicit stacking overrides, human/review holds, current required CI and
   promotion/deployment bindings. Never seed delivery receipts. Subsequent
   normal policy enable/disable edits do not repeat a protocol migration.
7. Reconcile Git, resume, then canary two dependent children with delayed parent
   delivery, a parallel overlapping child and a standalone task. Exercise an
   author-resolved conflict, a multi-input batch and a deliberately red split
   with its deferred fix. Capture each original full source OID and ancestry on
   actual `dev`, exact required CI and product/deploy health. Preserve candidates
   across restart; do not manually merge or file routine repairs to bypass the
   flow. Verify one shared demand/frontier and one shared waiting view.
8. Redrive only genuinely pending inventory entries. Observe each in-flight epic
   for a cycle, name one incident per remaining blocker and measure process
   stalls (withheld, fenced, frontier-excluded or awaiting evidence). Observe
   24 hours before broadening and seven days before the live SLO decision,
   reporting all eligible/pending/censored/excluded cohorts and segment times.

Rollback pauses admissions/writes, records all newly published/local heads,
retains every source and selects a compatible publisher/configuration that still
reads strict containment. Never reset `dev` or epic refs and never restore
receipt authority. If the old binary cannot read new operations, stay paused and
fix forward. Retain additive and historical audit tables through the rollback
window. Archival and destructive migrations need later explicit operator signoff.

## Matter Engine

1. The owning supervisor identifies every real Matter project/repository and
   product. `matter-engine-cpp`, `matter-engine-web`, `matter-engine-msvc` and
   `./tools/ci-windows.ps1` are historical fixtures. Neither their existence nor
   current policy is verified here. Keep the train off until that owner decides
   to enable it; this report cannot perform cross-project activation.
2. Complete the Agent Queue source/writer/CI/recovery inventory plus native-disk
   capacity for two full copies and build headroom. Record all clone, slot,
   common-dir, retained/candidate/fixer/cache/recovery paths; Git refs and remote
   heads; submodules/LFS; dirty, ignored and untracked assets; executable modes,
   ACLs and symlinks; services/restart triggers, mounts and environment/config
   path references. Measure cold/warm builds and I/O without replacing AQ's
   recorded test baseline. Confirm the proposed native root and actual owner.
3. Pause only the affected project's assignments and writers. Settle uncertain
   publications and preserve retained claims, preparations, workspaces and jobs.
   Make a full recoverable non-Git backup as well as a Git ref/object backup.
   A bundle does not contain ignored files or LFS content. A claimed dirty or
   linked worktree must be retired/recreated through supported AQ tooling or
   postponed; never patch `.git` pointers or `.aq/claim.json` in place.
4. Follow the existing [storage relocation runbook](../../guides/matter-engine-storage-relocation.md).
   An authorized local operator uses `aq project relocate --help`, previews a
   fresh native destination with the signed owner inventory and reviews the
   exact digest before apply. The tool must verify two independent copies and
   freshly fence writers, old config, policy, contents and remote heads before
   publishing path bindings atomically. Do not use raw SQL/config bypasses or
   delete a partial destination to force success. Retain it and use a fresh plan.
5. Recreate disposable slots through AQ, then verify every AQ Git/common-dir,
   retained/candidate/fixer/cache and running service path has no `/mnt/d`
   dependency. Separately authorize inventoried mount/service/editable-install
   changes. A worker never repoints the shared AQ virtual environment.
6. Run the actual product's declared local build/test/package checks on native
   storage, warm the cache with a full build and measure warm rebuild/I/O. Verify
   current trusted exact-candidate hosted/native checks, webhook/App access,
   branch protection and dev/epic/candidate triggers. A currently required
   Windows job needs its own native checkout/runner. An approved Linux-only
   policy must not inherit an obsolete Windows blocker. Unknown current policy,
   required runner or product publication health stays with a named owner.
7. Review the project playbook data: explicit delivery ref and current checks,
   stacking default, advisory epic checks unless explicitly required,
   `close_merge.author_assist: false` while checks exceed the author budget,
   speculation off for local/hybrid CI, and measured watchdog threshold
   `max(1800, 2 × p90 batch CI)`. A 40-minute p90 gives an 80-minute threshold.
   Preserve actual release approvals. Enable only after the owner signs off;
   use existing package publication for a native library rather than inventing
   a website deployment.
8. Canary dependent/conflicting/split/standalone work and prove each original
   source on actual dev with current required checks and product health. Resume
   normal assignments only after the native binding and canaries are verified.
   Keep old mount data read-only through seven days of observation/rollback;
   delete it only after explicit backup and retention signoff.

Rollback stops only the affected writers, retains all new local/remote heads and
non-Git work, and uses the supported reverse relocation preview/apply to a fresh
verified checkout containing the newest heads. Never rebind to a stale old copy
and overwrite new work. Verify the reverse plan's current digest and stopped
writers. Storage relocation precedes flow activation and has its own rollback
decision. The owning Matter supervisor files and executes the local sequence.
