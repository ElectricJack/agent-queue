# Recovering legacy open PRs the train can never seat

Status: implementation, task `fresh-willow-73`, 2026-10-01. Follows
[continuous delivery](2026-09-30-continuous-delivery-design.md), which decided
that legacy PR #660 is delivered by "a fresh explicitly authorized repair root
carrying source and repair-child ancestry, followed by proven supersession of
covered legacy work; no parent receipt is fabricated".

The user authorized finishing every existing feature and bugfix branch through
continuous integration and closing only PRs whose work is proven to be on the
default branch. The live train (`integration-batch-99c930…`) already carries
the reviewed backlog; four open PRs had no supported route at all.

## Diagnosis

Observed against `origin/main` `3a4cd41dd`.

| PR | Task | Why the train cannot seat it | Git facts |
|---|---|---|---|
| #660 | `steady-delta` (COMPLETED chore root, PR approved at `bc34e7b1`) | Its only child, `development-repair-239c…`, makes it a parent, and it has no parent verification. `eligible_root_page_on`, the review source and `materialize-root` all require one; none may be fabricated. | `bc34e7b1` conflicts with main. The retired development publisher filed a three-generation repair chain, none delivered: `8abe63e9` (`development-repair-239c…`, the child) ⊂ `0cacb3c11` (`development-repair-6f41…`, root) ⊂ `7f57d5727` (`development-repair-57df…`, root). Their remote branches are deleted; the commits survive only in the shared checkout under local `aq/development-repair-*` branches. |
| #696, #697 | `clear-summit-30.2`, `.1` (COMPLETED bugfix children of the COMPLETED-fail AQ-3 root) | `repo_id` is null and they have no branch origin or checkpoint, so they are not train members and never can be: `redrive-child` refuses (no checkpoint predating collection), `bind-legacy-repositories` refuses (sources not on main). | `305aee2b` and `61716ef4` each merge cleanly onto main alone and conflict with each other in `src/commands/object_loop_commands.py` and `tests/test_object_loop.py`. |
| #687 | none (operator branch `fix/repair-may-edit-reviewed`) | Untracked: no task, no authorization record. | Its single commit `51e66833` has the same stable patch ID (`a762c482…`) as `578b4681c`, an ancestor of main (same author, date and message). `merge-tree` conflicts only because main later edited the same hunks. |

No completion provenance ref (`refs/heads/aq-provenance/completions/*`, 376
scanned) names any of these heads as its source, so the delivery evaluator
cannot prove them, and `migrate-provenance` refuses a train project. Every
refusal above is a guard working as designed; none is bypassed here.

## Decision

### Deliver through fresh carrier roots

A *carrier* is an ordinary root task in the train project whose branch merges
the legacy heads exactly — `git merge --no-ff <sha>`, never rebase, squash,
cherry-pick or amend. It then takes the normal path with no new mechanism:

1. its close advances the leaf checkpoint and `RootPullRequestReconciler`
   opens its PR;
2. source CI runs on its exact head (`conflict` is admitted under
   `repair.conflict_scope: batch`; red files a source-CI repair root);
3. authorized admission records exact remote-head/tree evidence — a
   feature/bugfix carrier directly, any other type only once the supervisor
   adds its id to `root.authorized_task_ids`;
4. a batch merges it, batch repair resolves conflicts, candidate CI gates it,
   and main fast-forwards to the exact green candidate.

Because every legacy head is an ancestor of the carrier head, main then
contains each one. GitHub marks a PR merged when its exact head becomes
reachable from the base branch; this repository's member PRs already close
that way (`mergedBy` is the train App at push time). So #660, #696 and #697
close as merged by construction, and only if their exact heads landed.

Filed carriers (root follow-ups, so they do not block this task):

- `calm-glacier-52` (bugfix): merges `305aee2b` and `61716ef4` and resolves
  their mutual conflict, preserving both stop paths and both regression sets.
- `swift-torrent` (chore): merges `7f57d5727`, which carries `0cacb3c11`,
  `8abe63e9` and `bc34e7b1`. It keeps the source's type, so its admission is
  the supervisor's explicit `root.authorized_task_ids` decision — a train
  policy change this task requests and does not make.

Carriers do not touch the legacy tasks, their completions or their branches,
and `clear-summit-30` (AQ-3) is retried separately on the delivered base.

### Settle the legacy tasks with existing proofs

After a carrier lands, the legacy tasks are proven by Git through the
existing controls, unchanged:

- `aq integration bind-legacy-repositories agent-queue` binds
  `clear-summit-30.1/.2`, whose sources are now on main;
- `aq integration adopt-legacy-deliveries --project-id agent-queue` adopts
  them by `branch_tip`. `development-repair-239c…` has no branch or recorded
  commit left; its retired journal source proves it `content_equivalent`, or,
  failing that, `--supersede development-repair-239c… --by 8abe63e9… --reason …`
  names its own head, which the control refuses unless it is on main.

No receipt, verification or approval is written for `steady-delta`; its
delivery is Git's answer for its head.

### Close what GitHub cannot: `aq integration close-delivered-pr`

GitHub's automatic merge only recognises the exact head. A PR whose change
landed under other commits — #687, a carrier that had to rebase, an operator
cherry-pick — stays open, and until now the only way to close it was `gh` by
hand, outside AQ's authority and audit. The new operator/supervisor control
closes an open PR of the project's designated repository only on Git proof:

```text
aq integration close-delivered-pr PROJECT_ID PR_NUMBER                      # dry run
aq integration close-delivered-pr PROJECT_ID PR_NUMBER --apply --head HEAD_SHA --reason REASON
```

It reads the PR through the repository's GitHub credential, refuses a PR
whose base is not the default branch or whose head is not a branch of the
designated repository, fetches the retained repository, checks that the
remote head branch is the PR head, and tries three proofs in order:

| Proof | Meaning |
|---|---|
| `ancestor` | The PR head is reachable from the default branch. |
| `patch_equivalent` | The PR is a linear series (no merge commit since its merge-base) and `git rev-list --cherry-mark` finds a patch-identical commit on the default branch for every commit. The matching default-branch commits are listed. |
| `content_equivalent` | `git merge-tree --write-tree` of the default branch and the PR head yields the default branch's own tree: merging changes nothing. |

| Outcome | Meaning |
|---|---|
| `would_close` | Proven; the dry run reports the proof, head, target and tracking tasks. |
| `closed` | Applied: proof comment posted once (marker `aq-delivered-pr:<number>:<head>`), PR closed, `integration.pr_closed_delivered` recorded. |
| `nothing_to_close` | The PR is already closed or merged. |
| `undelivered` | No proof reaches it; `undelivered` names the commits left, the conflict or the files merging would still change. The PR stays open. |
| `changed` | The PR head moved, or differs from `--head`. Run the dry run again. |
| `blocked` / `not_eligible` / `not_found` / `invalid` | Repository, route, Git or argument problems, with the reason. |

Applying requires the head the dry run reported and a reason, re-proves under
the retained repository's lock, and re-reads the PR head immediately before
closing. Main only moves forward, so a proof cannot be invalidated by main
afterwards. A patch-equivalent proof records *when* the change was delivered;
a later deliberate change on main does not undo that delivery. It never
changes a task, completion, receipt, checkpoint or branch: tracking tasks
(`tasks.pr_url`) are reported, not modified.

## Operator sequence

```bash
# Carriers: route calm-glacier-52 and swift-torrent; allowlist swift-torrent
# (supervisor; policy CAS against a fresh generation)
aq integration status agent-queue --control-only
aq project set agent-queue integration-policy '<policy with swift-torrent in root.authorized_task_ids>' \
    --expected-integration-generation <generation> --reason "explicitly authorized #660 carrier"

# After each carrier's batch promotes
aq integration close-delivered-pr agent-queue 660    # expect nothing_to_close (merged) or would_close
aq integration close-delivered-pr agent-queue 696
aq integration close-delivered-pr agent-queue 697
aq integration bind-legacy-repositories agent-queue
aq integration bind-legacy-repositories agent-queue --apply --reason "carrier calm-glacier-52 delivered"
aq integration adopt-legacy-deliveries --project-id agent-queue --dry-run
aq integration adopt-legacy-deliveries --project-id agent-queue
```

## Tests

`tests/test_integration_pr_delivery.py` drives the control against real Git
repositories and a recording GitHub double: each proof, an undelivered
conflict and diff, a merge series that is not patch-proved, moved and
mismatched heads, closed PRs, fork and wrong-base PRs, the exact-head apply
with one idempotent marker comment, the recorded event, and the operator
authority check. Contract, scope, CLI and supervisor-grant tests cover the
registration.
