# Matter Engine storage relocation — owner runbook

**Fixture-only handoff.** `matter-engine-cpp`, `matter-engine-web`, the historical
`matter-engine-msvc` runner and `./tools/ci-windows.ps1` are examples, not a verified
live inventory. No live Matter repository, service, configuration, database or
shared virtual environment was moved by the implementation task. The Matter
train stays disabled until its owning operator explicitly enables it.

The owner must identify the real repositories, project IDs, checkout paths,
runner/check producers, release/deploy contract and responsible people. A native
engine library need not have a website deployment. An unknown required runner or
deployment contract belongs to that owner; an obsolete runner removed by the
current approved playbook is not a blocker.

## 1. Inventory and stop writers

Record and sign an inventory with the current project playbook digest and check
set; every repo and remote/default/delivery branch; local-only/provenance refs;
worker/common-directory identities; retained integration and recovery stores;
submodules and LFS objects; dirty/untracked/ignored assets; symlinks, ACLs and
executable bits; services, launchers, environment/mount/path references and build
tools. Record disk capacity for two full copies plus build headroom, and cold and
warm build/test durations. Do not replace AQ's recorded test baseline.

Pause admission through the project command and disable/drain its train. Settle
uncertain publications and active jobs, reservations, sessions and preparation.
Use supported workspace retirement/recovery commands for claimed or linked
worktrees. Preserve their data and identities; do not edit `.aq/claim.json`,
`.git` pointers or delete lock files. Stop only this project's services and their
restart triggers. A claimed dirty worktree postpones cutover.

The tool refuses ambiguous shared paths, shared compiler caches, populated
unpartitioned recovery roots and external Git/link dependencies. Resolve these
with the owning operator before retrying. It cannot infer which project owns
unlabelled legacy material. Materialize submodules and linked repositories into
independent checkouts, using supported credentials and verified local refs/LFS,
or retire disposable slots for later recreation. Fetch and retain the latest
origin heads before preview. Keep a full independent recovery copy of dirty work;
a Git bundle alone omits working files and LFS data.

## 2. Review and apply one exact plan

Run from an authorized local operator context after the operator has deployed
the schema revision. A worker slot never migrates the operator database. Check
`aq project relocate --help` for the structured JSON argument syntax. These
commands describe a disposable fixture, **not permission to execute on live
Matter storage**:

```bash
aq project relocate --project-id matter-engine-cpp \
  --destination /home/OWNER/relocation/matter-fixture-001 \
  --owner-inventory '{"fixture_only":true,"services":[],"owner":"fixture-owner"}' \
  --json
```

An empty services list asserts there are no affected services. A real inventory
must enumerate them with `name`, `stopped: true`, owner and path/settings evidence.
Include the signed inventory reference as another field. Preview creates nothing.
Inspect all planned paths, manifests, capacity, policy and blockers. The proposed
native root must be confirmed by the owner; paths beneath `data/<digest>` are
intentional. The tool materializes independent byte-verified checkouts and stores
there and preserves separate full backups under `backup/<digest>`.

Refresh missing or unfinished exact-SHA evidence through the normal trusted CI
observer before apply. Final evidence uses the train's `ExactChecks` contract. Preserve the
full current required hosted/native set. If current policy requires native
Windows build/test/package validation, keep that runner's own native checkout.
If the approved current policy is Linux-only, do not restore a historical Windows
gate. The fixtures cover both cases and reject evidence from a foreign producer.

```bash
aq project relocate --project-id matter-engine-cpp \
  --destination /home/OWNER/relocation/matter-fixture-001 \
  --owner-inventory '{"fixture_only":true,"services":[],"owner":"fixture-owner"}' \
  --apply --expected-digest DIGEST_FROM_PREVIEW --json
```

Apply rechecks fresh writer/claim/process/config/policy/content/remote fences and
publishes repository, workspace, cache and retained-store paths atomically.
It never enables the train or edits global config. A dirty worktree retains a full
verified recovery copy and refuses apply. Resolve dirty work through its owner,
then use a fresh destination/preview. Never remove a partial destination to force
a retry; retain it for recovery and choose a fresh root. After an ambiguous
response, retry the same digest: a committed binding returns `already_applied`.

## 3. Validate the native path before activation

Keep admission paused. Recreate disposable slots through AQ at the new native
roots. Use the receipt and current binding to verify all repository/workspace/
retained/candidate/fixer/cache paths and each Git common directory. Inspect copied
Git configuration and environment references as well as service paths: none may
depend on `/mnt/d`. Correct authorized service/mount/editable-install settings as
separate inventoried operator changes. Never repoint AQ's shared worker venv from
a task slot. Global `data_dir` remains a default for unrelated projects.

Run the project's declared local build/test commands on the new checkout, warm
the shared compiler cache with one full build, and record the warm rebuild and I/O
measurements. Obtain full current required exact-candidate hosted/native evidence.
Verify App/webhook access, trusted producer identity, branch protection and dev,
epic and candidate triggers (the historical fixture's `main`/`aq/**` triggers are
not evidence that a real dev branch is covered). Any failure keeps admission and
the train off; use reverse relocation if the native storage is unsuitable.

Configure delivery/default targets, current required checks, stacking, dispatch,
watchdog and author-assist budget in the approved project playbook. Derive the
watchdog from measured CI (40-minute CI suggests 80 minutes). Keep
`close_merge.author_assist: false` while checks exceed the budget, speculative CI
off while builds share this machine, and epic checks advisory unless the current
owner policy says otherwise. Preserve release approvals and actual package or
deploy publication. Only the owner enables the stage-2 playbook.

Canary a dependent chain, a conflict and a red batch. Prove exact sources on dev,
required platform checks and actual product health before resuming claims. Retain
old roots read-only and both recovery generations for at least seven days. Delete
only after the owner verifies retention, recovery and the observation results.

## Reverse relocation

Pause and drain only the affected project again, keeping the train disabled.
Preserve new dirty/ignored/local-only work and fetch/retain the newest remote
heads. Preview **current storage** into a fresh rollback root on native storage,
then apply that digest through the same command. This publishes generation N+1
with new work included; it does not switch to the stale original checkout. Dirty
reverse apply makes a full backup and refuses until the owner settles that work.
Revalidate local/platform checks and restore separately inventoried services.
Do not delete either generation during recovery.

## Fixture acceptance

`aq test tests/test_project_relocation.py` covers pure preview, atomic path-family
publication and reader snapshots, dirty/ignored/LFS/ref preservation, remote-head
races, stale configuration and policy, live claims/processes, concurrent applies,
precommit crashes/retry, shared ownership refusal, reverse preservation of new
work, current Windows-required and authorized Linux-only checks, and unrelated
project defaults. It does not certify a live Matter inventory or perform the
owner's migration, build, deployment or activation.
