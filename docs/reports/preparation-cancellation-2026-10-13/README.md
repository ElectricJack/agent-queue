# Preparation cancellation investigation — wise-stone-87

## Finding and scope

The published starting point was `d63fd995723b6377ed79146bdf3d381adb47f6f7`.
A disposable reproduction showed that cancelling `_arun` killed only the Git
leader: a child with independent standard streams could remain alive and write
after cancellation returned. `_arun_git_result_unlocked`, `_arun_subprocess`, and
setup-command cancellation also lacked equivalent process-tree ownership.
Cancellation during subprocess creation could lose the returned handle. These
are confirmed code defects, not proof of the historical Matter Engine cause.

The reported three Matter slot-3 `TimeoutError`s, WSL `/mnt/d` checkout, and
operator timeout change from 120 to 600 seconds are supplied incident evidence.
This worker did not inspect or modify that host, configuration, worktree, or
claims. The missing slot and historical branch/DB mismatch remain unqualified;
no live recovery, deployment, or task unpause is claimed.

## Deadline semantics on this source

| Setting or deadline | Actual owner and behavior |
| --- | --- |
| `swarm.prepare_timeout` | Reconciler stale-preparation threshold. An attempt still registered in `claim_preparations` is skipped. It does not cancel an in-flight Git command. |
| `worktrees.slot_reset_git_timeout_seconds` | Context-local budget for each Git operation in slot reset; explicit per-call deadlines take precedence. |
| `worktrees.setup_timeout_seconds` | Deadline for each setup command, not the complete preparation. A safely stopped setup timeout is logged and remains nonfatal. |
| `aq task claim --wait` | Frontier/long-poll wait; the preparation waiter is shielded. It is not an outer checkout timeout. |
| External cancellation / nested `asyncio.timeout` | Cancellation must settle subprocess startup and children before leaving the owning operation. |

Increasing a timeout can change which work finishes within its budget, but cannot
establish the source of a recorded exception or repair lost process ownership.
A killed checkout can leave partial index/working-tree changes or an `index.lock`.
This patch does not remove that lock, roll back source, or assume a clean HEAD.

## Implemented boundary

On Linux/WSL, local Git commands and worktree setup own a new process group.
Startup is shielded; cancellation and timeout kill and settle the group within a
separate five-second cleanup margin, including repeated cancellation. Late startup
handles stay owned if the margin expires. Zombie-only groups are quiescent;
unobservable process state is a failure. Git automatic maintenance is disabled
for these commands through their environment to avoid its intentional detachment;
existing command configuration is preserved. This does not edit repository config.
Normal success also settles background members of the group. On other platforms,
ordinary success is preserved and interruption without group proof fails closed.

Unproved cleanup is distinct from an ordinary Git command failure. Best-effort
Git probes, slot abort/default-branch selection and salvage must propagate it.
For an exact held preparation, the existing scrubbed, bounded formatter records
type, message, stage, cause chain and claim identity, now including acquisition
generation. A separate `prepare_cleanup_unproved` hold retains the first failure
identity. Claim expiry/retry and integration owner handoff cannot release or reset
that held source. Later failures cannot overwrite a newer epoch's record. No
second reason formatter, automatic hold clearing, or live retry-counter reset was
added.

The durable hold covers claimed preparation. It is not a general process registry
for unclaimed initial slot provisioning. Deliberately detached operator setup
commands, daemon/host death before recording an uncertainty, and future supported
hold lifting require separate process/source evidence. Automatic live repair is
still outside the demonstrated boundary.

## Disposable evidence

The regression fixtures use real temporary repositories and bounded barriers:

- Git post-checkout child writers with independent streams: cancellation, nested
  timeout and delayed handle creation across all three async command helpers;
  repeated cancellation cannot release the child into a successor checkout.
- Cancellation while a real smudge filter holds checkout: preserves HEAD, refs,
  index, `index.lock`, untracked work and claim bytes; a tree-changing successor
  refuses the lock rather than deleting it.
- Setup parent/child termination on cancellation and on its own timeout.
- Startup exceeding its cleanup margin returns explicit uncertainty, then retains
  ownership of the late process handle until cleanup completes.
- Claimed preparation cancelled before checkout, after checkout and after claim-file
  creation: original identity is retained until normal reconciliation, the next
  epoch succeeds, and stale completion cannot overwrite it.
- Unknown cleanup retains staged/untracked work, source refs, claim file, workspace
  generation and owner attachment across expiry, retry and all owner handoff paths.
  The fixture also checks secret scrubbing and unchanged retry budget.

Focused regression run: 22 passed. The area and real disposable swarm receipts are
recorded below and on the task after they finish; no whole suite is required.

## Incident closure and recovery evidence still required

Use the existing supervisor escalation and related supported recovery work; do
not duplicate it. To attribute or repair the incident, capture the loaded source
SHA, complete bounded traceback with the timer owner and preparation stage, the
exact task/session instance/claim epoch, workspace acquisition generation and
registered path, Git common directory/HEAD/index/source refs and operation pins,
and qualified process-group/provider quiescence for that same attempt. A missing
marker, a deadline passing, zero claims, or a detached clean HEAD does not supply
that proof. Preserve unknown, foreign and deferred source until the supported
recovery owner can act on those facts.
