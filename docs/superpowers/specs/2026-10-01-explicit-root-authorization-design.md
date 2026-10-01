# Explicit per-root train authorization while the train runs

Status: authorized implementation, task `keen-beacon-16`, 2026-10-01.

## Problem

A train project with root `admission: authorized` admits completed `feature` and
`bugfix` roots, plus the task ids its policy lists in `root.authorized_task_ids`
(`ReviewEvidenceProducer._authorization_on`). The user explicitly authorized three
completed roots of other kinds for delivery (`quick-flare-50` PR #711,
`wise-horizon-85` PR #694, `swift-torrent` PR #756). The only supported way to add
them was `integration configure` with a new policy, and `configure` runs only while
the project is fully disabled and drained. Continuous delivery never drains: a
reserved, unclaimed branch owner counts as active work, and follow-up roots keep
starting as earlier workers close. Stopping the train to change one allowlist is
not acceptable.

## Why not change the policy while the train runs

The project policy generation is an admission fence, not only an audit counter.

- Every `authorized_task` review evidence row carries the policy generation, and
  sealing admits it only while that equals the project generation
  (`scheduler.py`, `_eligible_members`).
- Every `integration_source_ci` row carries the generation it was observed under;
  sealing ignores rows from another generation, and a CI repair whose source row
  is stale withholds the whole repair chain. `_record_integration_source_ci`
  refuses an observation whose generation moved.
- Legacy suppression and rollout transitions are keyed by generation.

Any policy edit must compare and swap that generation (`configure`,
`cas_project_integration_control_on`). Adding three task ids that way would
withdraw every eligible source until the review poller re-observes each one, would
start new transition and suppression records, and would open a general route for
editing admission mode, required checks or repair policy while the train runs.
Sealed batches and operations keep their frozen `policy_snapshot`, so the
generation bump would not change them, but it would disturb everything that is
waiting to be sealed. The task asks for exact per-task authorization evidence when
the generation route touches existing fences, and it does.

## Design

An operator's explicit authorization of **one exact source** is recorded as its own
immutable row. Admission consults that row only where it would otherwise consult
the kind allowlist.

### Record: `integration_root_authorizations`

Append-only. One row per exact source identity:

| Field | Meaning |
|---|---|
| `id` | `root-authorization-<uuid5>` over the identity below |
| `project_id`, `task_id`, `repository_id` | Project, root task and designated repository |
| `source_base`, `source_head`, `generation` | The exact reviewed source: the branch-origin base, the head and the checkpoint generation (the same key review evidence uses) |
| `review_kind`, `pr_url`, `task_type` | Leaf/parent, the PR and the task's kind when it was authorized |
| `policy_generation` | Project generation when it was authorized; audit only, never a fence |
| `operator_id`, `reason`, `created_at` | Who authorized it, why, and when |

A unique constraint on `(task_id, repository_id, source_base, source_head,
generation)` makes the row a single exact grant. There is no update or delete path.
A new head, base or checkpoint generation is a different source and needs its own
authorization.

### Admission

`_authorization_on` keeps every existing check and changes only the kind check:

```
kind admitted = task_type in {feature, bugfix}
             or task_id in policy.root.authorized_task_ids
             or an exact integration_root_authorizations row matches
                (task_id, repository_id, source_base, source_head, generation)
```

Everything else stays as it is:

- the project must be in train mode with this repository designated, and the root
  must be COMPLETED with a verified exact source (`_pull_request_source_on`);
- a `hold:*` label or an open gate on the task refuses authorization;
- the latest review of the exact source being `rejected` refuses it;
- root admission must be `authorized`; under `reviewed` admission a human review is
  still required, and the authorization only gets source-CI observation past the
  kind check;
- the review poller still proves the remote head and tree in Git before writing
  `authorized_task` evidence, and still tags it with the current policy
  generation;
- sealing still requires that evidence to match the current generation, exact
  green (or batch-scope `conflict`) source CI at the current generation, and every
  source in a repair chain to stay an exact eligible member.

No policy, generation, task type, review evidence, batch or operation snapshot is
written. An active batch, its frozen policy and its members are never revisited.
Unrelated roots of other kinds stay refused.

### Command: `integration_authorize_root`

```
aq integration authorize-root TASK_ID                                # dry run
aq integration authorize-root TASK_ID --apply --head HEAD_SHA --reason "..."
```

Authority is `integration_operator` (the local operator or a live supervisor of
the task's project), the same as `integration materialize-root`. The dry run
reads one snapshot and reports the exact source: `task_type`, `repository_id`,
`pr_url`, `base_sha`, `head_sha`, `generation`, `review_kind` and the project's
`policy_generation`. Applying needs the head the dry run reported and a reason. It
re-reads everything under the project hierarchy lock, compares the head
(`changed` when it moved), inserts the row and logs `integration.root_authorized`
with the operator and reason, all in one transaction.

| Outcome | Success | When |
|---|---|---|
| `would_authorize` | yes | dry run; the source can be authorized |
| `authorized` | yes | the exact grant was recorded |
| `already_authorized` | yes | nothing to write: `authorized_by` is `policy_kind` (feature/bugfix), `policy_allowlist` (listed in the policy) or `grant` (this exact source already has one; a replayed apply lands here) |
| `changed` | no | the head differs from `--head` |
| `blocked` | no | a hold label, an open gate, a rejected review of this head, or root admission that is not `authorized` |
| `not_eligible` | no | not a COMPLETED train root with a verified exact source, PR and designated repository |
| `not_found` | no | no such task |
| `invalid` | no | malformed request (apply without head or reason, head not a full commit id) |

Withdrawing an authorization uses the binding controls that already exist: a
`hold:*` label, an open gate or a rejected review refuse the source at every
admission, whether or not a grant exists. Moving the head needs a new
authorization.

## Rollout

The supervisor deploys the change and runs operator migration `a00000000054`.
The worker does neither. For each user-authorized root:

```
aq integration authorize-root quick-flare-50
aq integration authorize-root quick-flare-50 --apply --head <head_sha> \
  --reason "User-authorized delivery of PR #711 (keen-beacon-16)"
aq integration authorize-root wise-horizon-85
aq integration authorize-root wise-horizon-85 --apply --head <head_sha> \
  --reason "User-authorized delivery of PR #694 (keen-beacon-16)"
aq integration authorize-root swift-torrent
aq integration authorize-root swift-torrent --apply --head <head_sha> \
  --reason "User-authorized delivery of PR #756 (keen-beacon-16)"
```

`<head_sha>` is the dry run's `head_sha`. On its next pass the review poller
observes source CI and writes `authorized_task` evidence at the current
generation, and the next periodic seal admits each source once its CI is green or,
under batch conflict scope, `conflict`.

## Tests

- `tests/test_epic_pr_review_evidence.py`: a chore root is refused without a grant
  and admitted with an exact one. The grant survives a policy generation CAS: the
  stale evidence drops out and the poller writes fresh evidence at the new
  generation. A grant for an older head, base or checkpoint generation admits
  nothing. A hold, an open gate and a rejected review still refuse a granted root.
  An unrelated chore stays refused. An already sealed active batch keeps its
  members and frozen policy when the grant is recorded. Under `reviewed`
  admission no evidence is written.
- `tests/test_integration_root_authorization.py`: the service's dry run reports
  the exact identity. Apply records one row and one audit event. A replayed apply
  returns `already_authorized` and writes nothing new. A moved head is `changed`,
  and holds, gates, rejections, `reviewed` admission, feature/bugfix kinds and
  allowlisted ids get their outcomes. A project without a train or a root without
  a source is `not_eligible`.
- `tests/test_integration_operator_controls.py`: operator/supervisor authority and
  the apply fence.
- Contract, CLI, scope, capability and generated-artifact checks:
  `tests/test_integration_contracts.py`, `tests/test_cli_integration.py`,
  `tests/test_shipped_profile_capabilities.py`, `tests/test_docs_sync.py`,
  `scripts/regenerate-generated.sh --check`.
