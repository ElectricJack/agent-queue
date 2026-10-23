# Project storage relocation

This implements the section 12 storage boundary of the fast/smooth integration
spec, with the supervisor's task `stark-willow-95.12` clarification: relocation
owns the missing project binding/CAS and its common readers. It does not activate
a project's delivery policy. The current project playbook, including an approved
Linux-only policy, determines required CI.

## Binding and command

`projects.storage_binding` is nullable JSONB. Null keeps configuration defaults.
A published binding contains a monotonically increasing `generation`, exact
`plan_digest`, recovery receipt, prefix path translations, compiler cache and
previous binding. Repository and workspace columns, the optional project root,
and the binding change in **one PostgreSQL transaction**. Global `data_dir` and
`workspaces.build_cache` configuration are never rewritten. Longest-prefix
translation maps retained Git stores, branch recovery stores, warm job/fixer
roots and compiler caches. Jobs and session launches resolve this same binding.
A consumer that needs several path families together uses `snapshot()` with a
repeatable-read transaction; independent reads spanning a commit are not a
multi-statement snapshot.

`aq project relocate` dispatches through `CommandHandler`. It requires local
operator authority. Preview returns a digest and plan without filesystem or DB
mutation. Apply takes the exact preview digest; the destination must be a fresh,
canonical native Linux root. `/mnt/<drive>` and Windows/network filesystem types
are refused. Existing unowned destination content is never adopted.

The plan includes database rows, effective cache/default paths, current compiled
project playbook, explicit stopped-service inventory, Git state and complete
SHA-256 file manifests. Manifests preserve ignored/dirty/untracked material,
Git object/ref/reflog databases, LFS data, relative internal symlinks, modes and
extended attributes (including Linux ACLs). They do not promise preserved inode
numbers, owners, hardlink identity or timestamps. Special files, external links,
Git alternates and external Git/config/LFS paths require owner materialization.
Current origin branch heads must already be locally present and reachable from
retained refs; apply rechecks them. LFS pointer files require matching local
object bytes. Remote credentials use the existing GitManager path.

## Preparation and fences

Admission must be paused and both current and desired train modes disabled.
Reservations, live sessions/preparations, tasks, jobs, branch owners, publication
leases, claim files or live processes refuse cutover. A linked worktree or
submodule `.git` file is not moved: the owner must retire it or materialize an
independent checkout. The command never patches claim files or Git worktree
pointers. Shared caches/repository paths and populated unpartitioned legacy
recovery stores refuse; ownership must be resolved first.

Apply creates two independent byte-verified copies: recovery under `backup/`
and prepared storage under `data/`. No source is removed and no hardlinks to the
source are created. Copies are fsynced before publication. A dirty worktree gets
a full recovery copy, then refuses publication. The operator must preserve and
settle the dirty work, then use a fresh plan/root. A bundle is not a substitute
for these full copies. Independent materialized repositories retain their exact
Git metadata; disposable linked slots must be recreated through AQ afterward.

Before commit, write fences on the project/repository/workspace/session/task/job/
owner/lease/check-evidence tables prevent a concurrent producer from crossing the
publication. The command rechecks the exact DB/config/policy snapshot, process
inventory, every source/backup/destination manifest, current remote heads, and
trusted exact-SHA check evidence. Evidence must match the current playbook's
producer, version and required names. Final exact-SHA evidence has the same
lifetime as the train's `ExactChecks` contract; unfinished evidence cannot pass.
Missing Windows CI blocks a Windows-required policy; a removed Windows check
cannot block an authorized Linux-only policy. No caller-supplied green assertion
is accepted. Hashing large trees and remote verification can hold these write
fences for a substantial interval; schedule the operator action accordingly.

External services remain the owner's responsibility: a supplied inventory does
not discover deployment contracts or prevent an unrelated process from starting.
Keep service launchers stopped for the whole operation. The process inventory
provides a fresh refusal fence, not a filesystem writer lock against arbitrary
external programs. Live configuration changes must likewise remain frozen.

## Failure and reverse relocation

The fsync/rename recovery receipt survives a failed preparation. A repeat apply
can reuse only byte-identical copies belonging to the same digest. A changed or
partial copy requires a fresh destination; nothing is deleted automatically.
A transaction failure leaves every active binding at the old generation. A retry
after a successful but ambiguously acknowledged commit reports `already_applied`.

Rollback is the same preview/apply operation against **current** storage with a
fresh destination. It creates generation N+1 and copies all new local work,
ignored assets, refs and fetched remote heads. It never rebinds to stale original
data. Preserve dirty new work first; dirty reverse apply also retains a full
backup and refuses publication. Keep old roots and recovery copies for the
owner's observation window (at least seven days for the Matter runbook).

Fixture coverage: `tests/test_project_relocation.py`. The operational checklist
is [Matter Engine storage migration](../../guides/matter-engine-storage-relocation.md).
