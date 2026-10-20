# Preparation cancellation investigation — wise-stone-87

## Current disposition — 2026-10-19

The task's current engineering direction supersedes the historical engineering
hold and requests completion of owned source work plus an operational handoff
to `supervisor-global`. The source fixes are published in
[PR 1520](https://github.com/ElectricJack/agent-queue/pull/1520), at
`2d6fecad0fd322fd19a0b37a261112351979996e`. The earlier sections below retain
their original evidence and dates; their no-retry language does not impose a new
hold on Matter work. Historical Matter causality and live repair remain unproved.

The completed source also includes retained-workspace checkpoint and stop cleanup,
successor fencing, and the native fleet merge. See the
[retained-workspace specification](../../superpowers/specs/2026-10-13-retained-workspace-stop-rework.md#source-ci-rework-epoch-4)
for that implementation and its validation history.

### Final source evidence

On October 19, this worker restored the exact published task branch using
`aq git pull` and queried
[hosted run 38505674226](https://github.com/ElectricJack/agent-queue/actions/runs/38505674226).
The run is completed/successful, names the exact `2d6fecad0` head, and all 16
jobs succeeded. Native ancestry retains both `b393925c8` and `69a45b259`.
The `src`, `scripts` and `migrations` trees match combined production
`9e3d97dc376b35262b5c8da11e3e57adda2a8d19` exactly.

The prior completion record `e9d314b5-e29c-40f1-919c-d59387a107bf` records
4,493 area tests passed, 16 skipped, one existing strict xfail; nine strict
performance tests passed with unchanged budgets; 19 targeted restart and
uncertainty tests passed; and a real disposable swarm passed all 20 scenarios.
Those are preserved historical receipts, not tests rerun for this documentation
handoff. Its log references are `/tmp/wise87-epoch4-area-final.log`,
`/tmp/wise87-epoch4-perf-final.log`, `/tmp/wise87-epoch4-fixture-restart.log` and
`/tmp/wise87-epoch4-e2e.log`. The recorded xfail is
`test_verify_skips_the_automerge_under_worktree_mode` (unfinished phase-three
auto-merge). This handoff changes documentation only.

### Operational handoff

Use existing `escalation-5068725d` and its recovery owners; no duplicate incident
or repair task is needed. `supervisor-global` owns the operator handoff. Matter's
own authorized operator/recovery owner owns all live Matter observations and
actions. This source worker has neither performed nor authorized those actions.

| Owner | Remaining action | Evidence to return |
| --- | --- | --- |
| `supervisor-global` / integration owner | Establish the delivery and loaded deployment of the published fixes through the configured integration path. | Delivered and running source SHAs, including whether the relevant fixes are present. A green source PR alone is not deployment evidence. |
| Matter incident owner | Attribute the actual failed preparation, using retained evidence or a fresh naturally occurring failure. | Scrubbed `prepare_failure`, complete bounded traceback, timer owner and stage, loaded source SHA, task, session instance, claim epoch, registered path and acquisition generation for the same attempt. If historical evidence is unavailable, record that limitation explicitly. |
| Matter recovery owner | Classify each current retained holder and use the supported control for its actual state after satisfying that control's checks. | Source and checkpoint receipt, current owner/generation and operation/job pins, provider/process quiescence, control result, and a qualified successor preparation proving reclaimed capacity without lost work or a late writer. |
| `supervisor-global` with Matter owner | Record the incident acceptance disposition separately from source completion. | Actual live recovery results and the remaining historical-cause limitation, if any; retain any required human review or external-action decision. |

For BLOCKED/FAILED work with an exact task binding, the supported recovery path
is `aq task stop <id>`; pending manual-pause cleanup uses `aq task pause <id>`.
These are instructions for the authorized owner after identifying the current
holder, not commands for this worker to run on foreign tasks. Both paths retain
source when their proofs fail. Dirty or unpublished work needs its durable full
checkpoint before capacity reuse. A task-null agent lock has no task-scoped stop
binding: the published implementation deliberately refuses it, and the existing
operator reconciliation owner must handle it. No general orphan-lock release
API or live task-null repair was delivered by this task.

A failure with unknown writer liveness, an unmerged index, or a changed identity
must be returned with the precise refused check. Do not infer safe release from
a clean/detached HEAD, absent marker, zero claims or an old stopped session.
Neither handoff nor fixture success supplies the missing historical evidence.
The owned source and handoff are complete; the original task's full live
acceptance remains unmet and must be reported as such at close.

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

The durable hold becomes effective when its failure transaction commits and covers
claimed preparation. It is not a launch journal or a proof against a concurrent
external release before that commit, and is not a general process registry
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

Focused regression run: **22 passed**. Final 19-file area run: **1,512 passed,
1 expected xfail**, no failures (604.81 seconds). Ruff, selection-catalogue
regeneration and `git diff --check` passed. The comparison baseline was the
recorded vault note `full-suite-baseline-2026-09-22.md`; no fresh baseline or
whole-suite run was performed.

The area command used the disposable PostgreSQL server on localhost:5534:

```sh
aq test tests/test_git_manager.py tests/test_git_manager_async.py \
  tests/test_git_mutex.py tests/test_git_identity.py tests/test_worktree_manager.py \
  tests/test_worktree_prepare.py tests/test_worktree_acquisition.py \
  tests/test_worktree_reaper.py tests/test_worktree_doctor.py \
  tests/test_claim_commands.py tests/test_session_reconciler.py \
  tests/test_workspace_attachments.py tests/test_integration_owner_retry_recovery.py \
  tests/test_integration_owner_recovery_sweep.py tests/test_integration_owner_recovery.py \
  tests/test_integration_workspace_handoff.py tests/test_preparation_failure.py \
  tests/test_stranded_work.py tests/test_selection_catalogue.py -q
```

Tested production commit: `18107effed870a7ee500dc6918455758fc7d7d09`.
Final documentation updates retain the same source trees:

- `src`: `c59c3bf2b5d6c7723b949aea7004c846581006b6`
- `scripts`: `b5c11dd9bdac5e806305d0b53ec344a3072f7196`
- `migrations`: `ea773ae7398671705711a4918a9dcf27328607d4`

The mandatory real PostgreSQL/no-LLM swarm **passed S1–S20**, including ordinary
train delivery with oversized history; the disposable daemon stopped cleanly.
The command was `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`, with
`AQ_E2E_HOME=/tmp/wise87-cancel-e2e-18107`, `AQ_E2E_PORT=18989`,
`E2E_DB_NAME=agent_queue_e2e_wise87_cancel_18107`, and test PostgreSQL port 5534.
No real providers, tmux agents, external messaging or operator database were used.
Logs: `/tmp/wise87-final-focused.log`, `/tmp/wise87-area.log`,
`/tmp/wise87-swarm.log`.

## Supervisor evidence received before close

The close guard delivered additional reports after verification. These were read
from this task/session's mailboxes; no foreign host, file, API or task was opened.

- `msg-2130c4b29bef47c4920c35b224776cc3` reports a nonretryable, 52-second
  `slot_reset_failed` attempt with no deadline and disagreement between repair
  attribution and the native session's preparing task. This does not establish
  that a timeout fired; changing the threshold is not an explanation.
- `msg-9ddba54617e346ba86abefa9a49accea` reports an AQ-local repair projection
  at epoch 11 while its named native session has no task/claim phase, and a
  reserved owner without a session/workspace. Projection attribution is not
  an exact active writer proof and does not authorize cleanup or replay.
- Supervisor comments `comment-a185cf27a47f4e7195ac20aa9e0a5316` and
  `comment-6f64aee1d03a4de5bba6c223329c133f` retain Matter-reported detached slots
  without markers, preparing sessions, and repeated own-branch handoff refusal.
  The latter reports Vivid slot 2 at `4427a094e83206baf40e682a012784f2646a5ec7`,
  with a zero-byte `index.lock` and no claim marker. Its cause and current writer
  ownership remain unproved; the reporter directed preservation and stopped
  further preparation attempts. The real-filter fixture demonstrates a possible
  surviving lock mechanism, not attribution of that particular lock.

These observations reinforce the negative cases in the preparation identity,
successor-epoch and lock-preservation fixtures. They do not change the tested
implementation or justify new operational retries, metadata changes, lock removal,
source cleanup, gate waivers, or duplicate diagnosis. The original acceptance
remains unmet. The earlier supervisor comment `comment-380d30e0b03f45ee9b91f7454958dca2`
also explicitly preserved the published ancestry and original acceptance.

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
