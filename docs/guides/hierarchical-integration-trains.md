# Hierarchical integration trains: operator rollout

<!-- aq:optional-mode -->
> **Optional strict mode, off by default.** This page documents a per-project
> integration mode a project may opt into. It is **not** the shipped default and
> it is not what this repository runs: AQ's configured policy here is
> [development integration](development-integration.md) — batched delivery with
> no pull request, no hosted-CI receipt chain and no per-parent verifier. Read
> [the integration concept page](../concepts/integration.md) for how the modes
> differ before following anything below.

This rollout is per project and defaults to disabled. It performs an in-place
schema upgrade on the PostgreSQL database the installation already uses. It does
not deploy, enable, or change GitHub configuration by itself.

If recovery supersedes a parent conflict intent, AQ updates the repair stage
and dossier to name the successor. To find delegates stranded by an older
installation, run `aq doctor --check integration.stale_repair_intents`.
The check is report-only. A local operator or the project's live supervisor
can prove and reserve a delegate's current candidate:

```bash
aq integration rebind-repair --task-id REPAIR_TASK --dry-run
aq integration rebind-repair --task-id REPAIR_TASK --apply --head CANDIDATE_SHA
```

Use the full `head_sha` reported by the dry run. The command refuses a stopped
writer, expired authority, changed candidate or moved remote target. Applying
records the reservation under the current intent; the attached repair session
then pushes with its current fence and closes. If the writer has stopped,
recover its attachment through the existing repair lifecycle first.

When a parent repair stage exhausts at the open conflict's old tip, its debug
successor keeps that conflict intent as its trigger. Its writer resolves and
publishes through `integration-resolve-conflict` and `push-conflict-resolution`
under its own fence, with no rebind. Any other debug stage carries the trigger
`stage-exhausted:<operation>:<ordinal>`.

A parent debug stage can be frozen on a commit the parent branch never
received: its retained handoff bound a stopped writer's local head, so every
delegate fails admission with `slot_reset_failed` ("repair branch no longer
descends from its frozen starting commit") and its `stage-exhausted` trigger
cannot resolve the open conflict. The same operators can rebind that detached
stage to the conflict at the published head:

```bash
aq integration rebind-detached-repair OPERATION_ID
aq integration rebind-detached-repair OPERATION_ID --apply \
    --stage STAGE --remote-head PUBLISHED_SHA --reason "..."
```

Pass the `stage` and `remote_head_sha` the dry run reported. The command
refuses unless the writer is detached and unclaimed, the stage is within its
unchanged deadline and attempts, the published head is the conflict's
expected target, and the frozen head is an unpublished descendant of it. Every
receipt must also sit in the published history. Apply changes only the stage's
starting commit, trigger and subject, and records the previous values in the
dossier's `detached_rebinds`. The branch, fence, budget, intent and gates are
left as they were. The same delegate is then readied, and it resolves the
conflict through the normal fenced publication path. Design:
[detached repair rebind](../superpowers/specs/2026-10-01-detached-repair-rebind-design.md).

If a debug stage has already expired without progress, but its stopped writer
completed a resolution that `release-owner` preserved, preview that exact commit:

```bash
aq integration recover-preserved-repair OPERATION_ID --intent INTENT_ID --candidate SHA
```

Run the returned `apply_command` after inspecting its source, target, parents,
tree, released fence and remaining attempts. Apply requires `--stage`,
`--released-fence` and `--reason`; it repeats every proof and publishes with an
expected-old compare-and-swap under a fresh collector fence. It consumes only
the audited two-parent merge. The expired deadline and consumed attempts remain
unchanged, the former delegate stays blocked, and normal parent verification is
still required. An exhausted attempt budget or human gate returns a specific
blocker. An interrupted push with an unchanged target is ambiguous and is never
blindly retried. See [preserved repair recovery](../superpowers/specs/2026-10-01-preserved-repair-recovery-design.md).

The command synopsis used below is:

```text
aq integration status PROJECT_ID
aq integration onboard-train PROJECT_ID [--write-policy PATH] [--write-trust-manifest PATH] [--write-workflow PATH] [--write-audit-workflow PATH] [--write-ruleset PATH]
aq integration app-verify PROJECT_ID [--policy FILE] [--repository-id ID]
aq integration flush PROJECT_ID
aq integration eject --batch-id BATCH_ID --task-id TASK_ID --reason REASON
aq integration enable PROJECT_ID --mode observe --expected-generation GENERATION --reason REASON
aq integration enable PROJECT_ID --mode train --interval-seconds SECONDS --expected-generation GENERATION --reason REASON
aq integration reconcile-unmaterialized PROJECT_ID --expected-generation GENERATION --reason REASON
aq integration bind-legacy-repositories PROJECT_ID [--apply --reason REASON]
aq integration close-delivered-pr PROJECT_ID PR_NUMBER [--apply --head HEAD_SHA --reason REASON]
aq integration waive-history PROJECT_ID --reason REASON --blocker-digest BLOCKER_DIGEST
aq integration resume OPERATION_ID
aq integration abort OPERATION_ID --reason REASON
aq integration retry-cleanup BATCH_ID
aq integration clear-stale-request PROJECT_ID [--apply --request-id REQUEST_ID --reason REASON]
aq integration redrive-root TASK_ID [--apply --head HEAD_SHA --reason REASON]
aq integration materialize-root TASK_ID [--apply --head HEAD_SHA --reason REASON]
aq integration authorize-root TASK_ID [--apply --head HEAD_SHA --reason REASON]
aq integration redrive-child CHILD_TASK_ID [--apply --head HEAD_SHA --reason REASON]
aq integration reopen-collection PARENT_TASK_ID [--apply --head HEAD_SHA --reason REASON]
aq integration record-noop CHILD_TASK_ID --expected-head-sha CHECKPOINT_SHA
aq project set PROJECT_ID integration-repository-id REPOSITORY_ID --expected-integration-generation GENERATION --reason REASON
aq project set PROJECT_ID integration-policy POLICY_JSON --expected-integration-generation GENERATION --reason REASON
```

The controls keyed by a task, operation, batch or reservation — `resume`, `abort`,
`retry-cleanup`, `eject`, `redrive-root`, `redrive-child`, `reopen-collection`, `record-noop`,
`reserve-owner`, `release-owner` — take no `project_id`, so who may run them is decided by
*whose target they name*: the project's own supervisor may run them against its own work, and
is refused anything belonging to another project. `PROJECT_ID`-keyed controls
(`enable`, `flush`, `waive-history`, …) are the reverse shape and are pinned to the token's
project directly. The scope model is [aq-surface §7.3](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

`reopen-collection` also recovers a suspended producer whose close detached its
workspace but left a `worker` reservation on the parent branch. The dry run
reports `kind: suspended_worker`, the current episode, operation, published head
and owner fence. Applying consumes the confirmed detach proof and transfers
ownership to that same operation's collector at the next fence. Collection
reconciliation retries this handoff automatically after restart. It refuses
live holders, operator holds, open gates, repair/verifier work and unresolved
external writes; it preserves the checkpoint, generation, operation and receipts.

For the `keen-ridge-24.1` incident, the operator deploys the recovery code, then runs:

```bash
aq --json integration reopen-collection keen-ridge-24.1
aq --json integration reopen-collection keen-ridge-24.1 --apply --head REPORTED_HEAD --reason 'Recover confirmed detached producer into current collector'
aq --json task show keen-ridge-24.1
aq --json task show keen-ridge-24.1.1
aq --json system delivery-receipts --source-task-id keen-ridge-24.1.1 --repository-id REPOSITORY_ID --target-branch aq/keen-ridge-24.1
```

Use the reported head and repository ID, checking episode
`fca39cce-5c3a-45e9-b677-7460d9f82477` and operation
`a143948f-0250-4631-806d-26450e850732` are still current. If the automatic pass
already recovered ownership, `nothing_to_reopen` is expected. Wait for ordinary
collection to create a receipt for reviewed head
`35742ce6f77ab633ba8f269c26c8b6a17db9660e`, targeting `aq/keen-ridge-24.1` with
that current `parent_operation_id` and `parent_episode_id`. The queued delivery
alone is not receipt evidence. Refusals require resolving the reported blocker,
then repeating the dry run; they never authorize database edits or bypassing CI.

Always take `GENERATION` and, for a history waiver, `BLOCKER_DIGEST` from a
fresh `aq integration status` result. A stale result is returned as stale; the
CLI never rereads and retries a mutation against a newer generation.

`aq doctor --check integration.blocked_collectors` reports managed parents that
are `BLOCKED` while their checkpoints still await children. For a displaced root
whose active episode still owns a reserved collector fence, run
`aq integration redrive-root <parent>`; a `would_collect` dry run can be applied
with `--apply --head <reported-checkpoint-sha> --reason ...` to restore `PAUSED`
collection. This resumes delivery through the existing collector. It leaves manual
holds, live writers and terminal failures guarded. An operation in `human_required`
requires its existing `aq integration resume <operation-id>` recovery instead.

## 1. Upgrade the existing backend

From the installed checkout, inspect the configured database without changing
it:

```bash
aq db current
```

If it is behind, stop here and have an operator run the following outside every
AQ worker/worktree session. `AQ_DB_SCOPE=worker` deliberately refuses it.

```bash
aq db upgrade
aq restart
```

Then inspect both schema and integration state. The integration checks are
report-only: `--fix` never changes a rollout mode, credentials, Git refs,
cleanup work, or schema.

```bash
aq doctor --check db.migrations --check integration.operational --check integration.unreviewed_prs
```

`integration.stranded_fences` reports branches whose ownership row is still
held `attached` (or `handoff_pending`) for a task that has no writer left — no
live session, no workspace lock, and not ASSIGNED/IN_PROGRESS:

```bash
aq doctor --check integration.stranded_fences
```

Such a row blocks every subsequent claim of the owning task with *"canonical
branch is not reserved by this task"*, and because the task stays READY the
scheduler keeps offering it. Its `--fix` runs guarded owner recovery, which
proves the old writer stopped and its checkout is safe before releasing the row.

`integration.missing_canonical_owners` reports READY/BLOCKED train producers
whose checkpoint identifies a canonical branch but whose owner row is absent or
released. After the old writer has stopped, a supervisor can restore one exact
reservation with `aq integration reserve-owner --task-id <id>`. This command
checks the task, checkpoint, materialized origin, session and workspace under
the project lock and refuses an unresolved or competing branch owner. A stopped
task's ordinary `aq task restart` performs the same check before moving it to
READY. Doctor reports this condition but does not reserve branches itself.

For an active repair delegate, the same `reserve-owner` command redispatches
the current stage, preserving its deadline and attempts. It transfers the exact
previous writer only with stop/detach proof, or reclaims a released reservation.
The integration loop retries interrupted handoffs after owner recovery.
`integration.missing_repair_owners` reports delegates stranded without their
reserved fence. An absent owner row still requires investigation; it is not
proof that an earlier writer stopped.

Once every cleanup item completes for a delivered batch, cleanup releases its
exact detached collector reservation. `aq integration retry-cleanup BATCH_ID`
also reconciles that release for batches cleaned up by older versions.

`aq doctor --check stall.sweep` reports `unmaterialized_train_pr` for a
COMPLETED train root with a PR but no checkpoint or live branch origin. The
GitHub review poller also warns when such a root has no eligible review source,
once per root and condition (again after the condition changes or the daemon
restarts), not on every tick.
For a **childless** legacy root, run `aq integration materialize-root TASK_ID`
to read its PR, remote branch head and merge-base with the default branch.
If the dry run returns `would_materialize`, apply with its exact `head_sha`:

```bash
aq integration materialize-root TASK_ID --apply --head HEAD_SHA --reason "legacy train cutover"
aq integration flush PROJECT_ID
```

The control rechecks the PR head and the task under the project lock before
recording the branch origin and leaf checkpoint. A root with children needs its
original parent verification evidence and is refused; the control does not
manufacture that evidence. Such a root, and any legacy PR the train has no
identity for, is delivered by a fresh root that merges its exact head; an
open PR whose work already landed is closed only on Git proof with `aq
integration close-delivered-pr` ([troubleshooting](integration-troubleshooting.md#a-legacy-pr-stays-open)).

Do not import another database during this release. PostgreSQL is the only
backend; the one-way carry-over importer
[`src/database/legacy_sqlite_import.py`](../../src/database/legacy_sqlite_import.py)
(behind `aq db import-sqlite`) refuses a non-empty target, so it cannot be used
to fold a populated installation into one that already holds integration state.
The two `migrate_sqlite_to_pg` copiers this page used to warn about no longer
exist. An in-place schema upgrade of the existing PostgreSQL database is
unaffected.

## 2. GitHub credentials: App mode or existing login

AQ reaches GitHub in one of two credential modes. Changing the mode requires a
daemon restart, and an App failure never falls back to a stored login.

**App credential mode is the production configuration.** When
`~/.agent-queue/config.yaml` sets `integration.github_app` (`app_id`,
`installation_id`, `private_key_path`), every GitHub read and write goes
through that App's installation token, and the train trusts three things: CI
from the policy's producer on the exact candidate, the `Agent Queue
Integration Attestation` check run the daemon publishes through its own App
once CI is green, and repository-side trust anchors that nothing in AQ
writes: the trust manifest `.github/agent-queue-integration.json`, the Actions
variables `AQ_INTEGRATION_ATTESTATION_APP_ID` and
`AQ_INTEGRATION_REQUIRED_CHECK_VERSION`, the `main` ruleset that requires the
attestation pinned to the App with no bypass, and the `main` push audit
workflow. The operator runbook, from prerequisites through cutover, rotation,
rollback and key rotation, with the command output to expect, is
[docs/config/app-mode-train.md](../config/app-mode-train.md).
`aq integration app-verify PROJECT` checks every anchor read-only, and the
functional preflight reads the same items, so each `fail` is a status blocker
of the same name.

In App mode every subject (a root candidate and each parent snapshot) must
carry the manifest in its own tree. AQ compares it on identity only
(repository, attestation App, attestation name and CI producer); the frozen
policy snapshot alone decides which checks are required, so no tree can add,
drop or rename one, and parent and root may require different sets. A subject
whose manifest is missing, oversized, malformed or names another identity is
refused: `aq integration status` shows `subject_trust_invalid` with the
subject, head SHA, cause and mismatching fields. Refresh that branch from the
default branch.

**Existing-login mode is for development installs without an App.** With no
`integration.github_app`, AQ uses `gh api` for repository, PR and CI
reads/writes, and the existing authenticated Git transport for exact
fetch/push operations. Run these as the same OS user that runs the daemon:

```bash
gh auth status --hostname github.com
git ls-remote https://github.com/OWNER/REPOSITORY.git HEAD
```

If the account is not logged in, run `gh auth login --hostname github.com`.
The account needs write access to the project repositories and permission to
read their CI results. AQ does not print or persist a copy of the gh token.
GitHub repository protections remain enforced; AQ does not bypass them. This
mode needs no App ids, private key, trust manifest, attestation or
`AQ_INTEGRATION_*` variables: AQ verifies the repository identity and exact
commit against authenticated GitHub results and records durable CI receipts
before guarded promotion, but GitHub itself enforces nothing about which
commit reaches `main`.

```yaml
integration:
  default_mode: pull_request
  merge_ci_policy: required
  merge_required_checks:
    - Tests (default)
```

The parent and root policy below declare the exact CI check names, version,
and producer: the producer App's numeric id as a decimal string, `"15368"` for
GitHub Actions. That is canonical in both credential modes, so a policy binds
under either without a rebind; a legacy `github-actions` slug still matches
under existing-login credentials, and App credential mode refuses it as
`ci_producer_not_numeric`. The producer identifies who ran CI, not a separate
AQ App. Historical internal names containing `app_client` are staged
compatibility names, not another transport to configure. See
[GitHub credentials](../reference/configuration.md#github-credentials).

The repository's CI workflow must run on the exact pushed parent and generated
integration branch commits, not only on `main` or a pull request's synthetic
merge commit. Declare every full-CI job required at each boundary. A train
promotion reuses the candidate's CI and runs no second full CI on `main`. In
App credential mode the `main` push audit workflow is the safety net: it
verifies the attestation on every push to `main` and runs full CI after one
that carries none. agent-queue ships `.github/workflows/main-attestation.yml`;
`aq integration onboard-train PROJECT --write-audit-workflow PATH` renders one
for any other project. Do not waive missing CI or substitute an empty
required-check set.

## 3. Bind reviewed shared artifacts, classes, and profiles

Parent and root routes name a shared system-scoped V2 artifact. AQ ships
`parent-integration` and `root-train` (`src/prompts/reviewed_playbooks/`,
seeded into `vault/reviewed-playbooks/`): the reviewed agent-queue graphs at
system scope. A system activation of either serves only a project whose frozen
policy names it. `aq integration onboard-train PROJECT` writes a policy that
references them and prints this whole rollout for one project; its runbook is
[docs/config/train-onboarding.md](../config/train-onboarding.md). The older
`hierarchical-delivery` and `root-integration-train` bundles were retired on
2026-09-11 and are not valid choices for a new project policy. See [Retired
factory playbooks](#retired-factory-playbooks) below before touching an
installation that still references them. To use a bundle of your own instead,
import and activate it once, then reference that exact artifact from each
project's policy with `scope: system` and an empty `scope_identifier`.
Schedules, repositories, CI requirements, repair budgets, and operation state
remain per project. Importing never activates:

```bash
aq playbook v2-import --path /srv/aq/reviewed/PARENT_PLAYBOOK_ID
aq playbook v2-import --path /srv/aq/reviewed/ROOT_PLAYBOOK_ID
aq playbook activate --playbook-id PARENT_PLAYBOOK_ID --artifact-sha256 sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --enabled
aq playbook activate --playbook-id ROOT_PLAYBOOK_ID --artifact-sha256 sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --enabled
```

Use the full hashes returned by import, not the sample hashes above. Confirm
the exact active artifacts and every referenced route before configuration:

```bash
aq playbook artifacts --playbook-id PARENT_PLAYBOOK_ID
aq playbook artifacts --playbook-id ROOT_PLAYBOOK_ID
aq system list-intelligence-classes
```

The policy is one JSON object. This example is structurally valid; replace its
sample hashes, identities, project ID, activation identities, classes and
checks with the exact imported and installed values. The classes are hints:
repair and verifier tasks are filed unrouted with them and the project's
router assigns their profiles. The deprecated `primary_profile_id`,
`verifier_profile_id` and `repair.debug_profile_id` fields are ignored in
stored policies and refused in a new one. Parent and root routes
are explicit; nothing is inferred at enable time. A project-specific override
may instead name a project-scoped artifact for that exact project. Other
project identities and agent/supervisor scopes are rejected. System activation
does not enable integration for projects that have no policy or remain disabled.

```json
{
  "version": 1,
  "parent": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "15368"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep-high"},
    "route": {"playbook_id": "PARENT_PLAYBOOK_ID", "scope": "system", "scope_identifier": "", "activation_id": null, "artifact": {"playbook_id": "PARENT_PLAYBOOK_ID", "artifact_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "schema_generation": 2, "contract_fingerprint": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "source_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
    "primary_intelligence_class": "standard-high",
    "verifier_intelligence_class": "standard-high"
  },
  "root": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "15368"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep-high"},
    "route": {"playbook_id": "ROOT_PLAYBOOK_ID", "scope": "system", "scope_identifier": "", "activation_id": null, "artifact": {"playbook_id": "ROOT_PLAYBOOK_ID", "artifact_sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "schema_generation": 2, "contract_fingerprint": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "source_digest": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
    "primary_intelligence_class": "standard-high",
    "verifier_intelligence_class": "standard-high"
  },
  "branchless_parent": "verifier",
  "on_failed_child": "block",
  "on_main_moved": "rebuild",
  "cleanup": {"max_attempts": 5, "retry_base_seconds": 30.0, "retry_max_seconds": 3600.0, "successful_source_refs": "delete", "failed_work_retention_seconds": 604800}
}
```

The optional `max_wait_seconds` (a finite positive number of seconds, default
`3600`) is the project's `max_wait`: the longest an undelivered integration
event keeps retrying before the outbox quarantines it. A quarantined event is
an explicit failed delivery: its `last_error` starts `retry_budget_exhausted:`
and it keeps every frozen destination and pin. Stored policies name the field
only when it differs from the default, so snapshots frozen before it existed
still compare equal. Projects without a hierarchical policy, development mode
included, use the default. The five event types no shipped playbook consumes
(`integration.root_delivered`, `integration.human_blocked`,
`integration.cleanup_pending`, `task.integration_configuration_blocked`,
`integration.branch_materialization_pending`) are not quarantined. They are
marked delivered with `last_error` `unsubscribed: ...`, but only when the
playbook runtime proves that no ready activation in the project's scope
subscribes to them. An unready or unloaded activation, a partially captured
fanout or paused playbooks all leave the event retrying instead.

Repository and policy changes are accepted only while the project is disabled,
fully drained, and has no active integration work. Bind one field, reread status
for the incremented generation, then bind the next:

Use `aq integration status PROJECT_ID --control-only` when only the current
generation, schedule, active batch and durable drain blockers are needed. This
read avoids historical Git delivery checks and external preflight. Its
`projection_kind: control` response leaves readiness unknown; use the full
status and candidate evidence for rollout and promotion decisions. A retained
worker claim is a drain blocker until its ordinary pushed completion/handoff
releases ownership.

```bash
aq integration status example
aq project set example integration-repository '{"id":"repo","url":"https://github.com/OWNER/REPOSITORY.git","default_branch":"main"}' --expected-integration-generation 0 --reason 'bind exact existing project repository'
aq integration status example
aq project set example integration-review-mode pull_request --expected-integration-generation 1 --reason 'require PR delivery review'
POLICY_JSON="$(jq -c . /srv/aq/reviewed/example-integration-policy.json)"
aq project set example integration-policy "$POLICY_JSON" --expected-integration-generation 2 --reason 'bind reviewed routes and policy'
aq integration status example
```

`integration-repository` creates a missing repository record or repairs the URL
and default branch of an existing project-owned record, then designates it
atomically. Its URL and branch must exactly match the project's existing
repository metadata; it never guesses a repository. It preserves existing
source paths and refuses IDs owned by another project. Use
`integration-repository-id` instead when the existing record is already correct.
Repository, review-mode, and policy changes all require the fresh integration
generation while disabled and drained. They are open to the LOCAL operator
and to a live, named supervisor session of the project holding the
`edit_project` grant (the shipped supervisor profile does), the same gate as
every operator integration control. A worker session is refused at scope.

Do not put rollout mode fields through `aq project set`; mode changes exist
only under `aq integration enable`.

## 4. Roll out one mode at a time

Keep `default-pipeline` enabled: its spec/proposal rules remain in use.
Hierarchy/train mode suppresses only its legacy per-branch final-review/merge
route. It no longer files per-task reviewers (automatic reviews were retired on
2026-09-09), so a completed child's approval evidence comes from the collector:
it proves the child's published head from Git and records `leaf` completion
evidence for exactly that head and tree. A reviewer's verdict, when one exists,
still wins: a rejected head or an open reviewer task holds the child. Retire the
project's `pr-merge-sweep` activation after cutover; keep the template available
for projects using legacy delivery. `ci-main-sentinel` remains a read-only
fallback observer of existing main CI and files repair PRs through the train.
`blocked-task-escalation` must defer integration-owned tasks to operation-level
recovery instead of generic task recovery or replacement repair budgets.
Failed delegates of active, escalated or human-required repair operations keep
inspectable recovery incidents with their stage attempts and deadlines, but do
not send `Task recovery: <delegate>` supervisor messages. Failure-event replays
and the recovery scan also archive older delegate notices without redelivery.
Integration parents and ordinary tasks keep their existing notifications.

When replacing project integration playbooks with shared system activations,
first disable/drain affected projects and verify there is no active operation.
Deactivate the exact project-scoped artifact hashes before activating the
system copies and update frozen policy references while disabled. Restart AQ
after the activation changes to reload the integration dispatch destination
cache, then restore the prior rollout mode. Archive superseded project source copies; keep retained
artifact and event history. Temporarily pause legacy review/merge dispatch
during that cutover so disabling integration cannot restart legacy merging.

First enter observe and clear every functional blocker shown by status. Observe
runs eligibility without scheduling or mutating Git:

```bash
aq integration enable example --mode observe --expected-generation 3 --reason 'begin observation'
aq integration flush example
aq integration status example
```

A parent that finished before the train has no parent collection and never
will, so its children cannot get train receipts. The same holds for a finished
parent whose collection was cancelled (switching the project to development
cancels parent operations but keeps their checkpoints). Status accepts a
terminal child of such a parent when the development publisher delivered it to
the default branch: that `delivered`/`adopted` development delivery, bound to
the child's latest completion, is its receipt. Any other such child is reported
as `missing_receipt` with cause `no_parent_collection`. Clear these with the
proof-based control. Dry-run it first; it is safe to repeat:

```bash
aq integration adopt-legacy-deliveries --project-id example --dry-run
aq integration adopt-legacy-deliveries --project-id example
```

It fetches the designated repository once and records an
`integration_legacy_deliveries` row for each child in either case:

- a development delivery lists the child, and the child's source commit or the
  delivery's published commit is an ancestor of the default-branch tip;
- the child's branch tip is such an ancestor;
- the work reached the default branch under other commits (cherry-picked,
  squashed or re-delivered): merging a delivery commit, the branch tip or the
  child's latest completion commit into the tip changes nothing
  (`content_equivalent`).

It lists every other child with its reason (`not_on_default_branch`,
`child_not_completed`, `parent_not_terminal`). For `not_on_default_branch` it
also reports, under `undelivered`, what merging the child's work would still
change: the commit examined, a diffstat and the paths, or a conflict. A child
of a parent that is still open is never adopted, because that parent's
completion needs real train receipts. A human settles the rest one child at a
time, each with `--reason '...'`:

- `--supersede TASK_ID --by SHA` when SHA, which must be on the default
  branch, re-delivered the work (`superseded`);
- `--retire TASK_ID` when the work was abandoned; the task and its branch are
  left as they are (`abandoned`);
- `--accept TASK_ID` when nothing is owed (`operator_accepted`).

Terminal hierarchy tasks delivered by the development publisher can still
carry a null repository ID. After leaving development mode, status reports
`repository_not_designated` for them. Preview bindings, then apply with an
audit reason:

```bash
aq integration bind-legacy-repositories PROJECT_ID
aq integration bind-legacy-repositories PROJECT_ID --apply --reason 'bind proven development deliveries'
aq integration status PROJECT_ID
```

This supervisor control binds only terminal hierarchy members with proof on the
designated repository: a development receipt for the latest completion
(`development_delivery`), an `integration_legacy_deliveries` row that
`adopt-legacy-deliveries` recorded, whether proven or decided with
`--supersede`, `--retire` or `--accept` (`legacy_delivery`, with the recorded
proof in `legacy_proof`), or terminal children that are all proven either way
(`delivered_children`). The preview lists every unproven member; applying
leaves those tasks unchanged. Each bound task gets an audit comment with its
proof and the operator's reason. Repeat the preview after adopting any missing
legacy child deliveries.

If status reports only `legacy_pr_merge_gate` blockers, an operator may make
that exact history inapplicable. Copy the current digest once, create the
immutable waiver, then pass the returned waiver ID to the cutover:

```bash
aq integration waive-history example --reason 'accept reviewed pre-cutover history' --blocker-digest sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
aq integration enable example --mode hierarchy --expected-generation 4 --reason 'enable recursive delivery' --waiver-id WAIVER_ID
```

Otherwise do not waive: fix the repository, policy, artifact, activation,
profile, intelligence-class, GitHub authentication, CI-policy, or runtime-wiring
blocker (in App credential mode also the manifest, hosted-variable and
protection blockers `aq integration app-verify` names).
In hierarchy mode, terminal children integrate children-first and the
parent is completed only after exact current-generation/head receipt and parent
verification. Root train sweeps remain off.

After observing recursive delivery, advance to train using the fresh generation:

```bash
aq integration status example
aq integration enable example --mode train --expected-generation 5 --reason 'enable root train sweeps'
aq integration flush example
aq integration status example
```

### Recover tasks created before repository binding

Tasks created while integration was disabled can have no repository binding or
branch-origin reservation. After a repository is designated and hierarchy or
train mode is enabled, recover only that legacy state with a fresh generation:

```bash
aq integration status PROJECT_ID
aq integration reconcile-unmaterialized PROJECT_ID \
  --expected-generation 5 \
  --reason 'bind pre-rollout unclaimed task graph to the designated repository'
aq integration status PROJECT_ID
```

`PROJECT_ID` is the AQ project identifier, not the designated repository ID.
The command is LOCAL-operator-only and runs under the project hierarchy lock.
It advances the generation and atomically binds every null-repository task in
the project, preserving task IDs, graph edges, dependency edges, and manual
holds while creating the normal immutable origin/checkpoint/outbox records.
It refuses the entire operation when any affected chain has an active claim,
an existing origin/checkpoint, a different repository binding, or crosses a
project. Do not repair these rows with SQL; resolve the reported ambiguity and
repeat from a fresh status generation.

Train mode checks the periodic window every 300 seconds by default and seals
all eligible root work into the next batch. To choose a different positive
per-project cadence, supply it with the train-mode generation CAS:

```bash
aq integration status example
aq integration enable example --mode train --interval-seconds 900 --expected-generation 5 --reason 'set fifteen-minute train cadence'
aq integration status example
```

The status `schedule.interval_seconds` and `schedule.next_due_at` fields show
the effective setting. Omitting `--interval-seconds` preserves an existing
schedule interval; changing it sets the next due time to the mutation time plus
the new interval without dropping an outstanding or coalesced request. The
option is invalid outside train mode and cannot cancel an active drain. Use
`flush` for an explicit sweep and never edit the schedule row directly. One
project cannot have overlapping active trains. A sweep whose frontier is
empty is not a train: the seal consumes its request and inserts no batch row,
answering `empty` with the `batch_id` `integration-empty:<request_id>`, which
`integration_release` confirms from the schedule (a replay of that seal answers
the same, even while a later train holds the project lease). Every nonempty
batch uses an ephemeral integration branch, including a singleton batch. Main
promotion is permitted only for the exact candidate OID already proven by the
configured CI producer; there is no post-main audit run. Ordinary task PRs retain the full-CI
fallback.

Successful integration/source branches are deleted by the default cleanup
policy. Failed forensic work is retained for `604800` seconds by default.

### No-code child receipts

A reviewer filed under an active parent is itself a child in the collection
episode. Its `pass --work-outcome no-op` close records the review verdict, but
the parent still needs a disposition receipt for the reviewer's own branch.
When parent readiness reports `receipt_missing` for that child, a local
operator can record the exact no-code disposition:

```bash
aq --json task show CHILD_TASK_ID | jq -r '.data.integration_delivery.checkpoint_sha'
aq integration record-noop CHILD_TASK_ID --expected-head-sha CHECKPOINT_SHA
```

The command requires the current passing `no-op` completion, and for a reviewer
it requires an approved review evidence row. It checks that the child head is
still its reserved base, resolves that commit's tree from Git, and writes a
receipt for the current parent episode. Repeating the command returns the same
receipt; a new no-op completion gets a new receipt revision. A playbook may
invoke the contracted `integration_record_noop` command when its policy grants
that exact capability. Worker sessions cannot invoke it.

### Root delivery in `task show`

A root task (one with no collection episode) is delivered by its train batch,
not by a parent. `aq --json task show` projects that delivery from the durable
root receipts: when the task's checkpoint has no episode, the projection lists
the `code` receipts on the repository's default branch whose batch member is
the task and whose `reviewed_head_sha` equals the current `checkpoint_sha` and
whose review evidence row carries the checkpoint's current `generation`. With
at least one such receipt the outcome is `delivered`; otherwise it stays
`working` with no receipts. A receipt for an older head or generation is never
counted, and the projection only reads; it never writes or rewrites receipts.

Before sealing, the train also asks the shared Git delivery evaluator whether
each candidate's exact completion source is already contained in the designated
repository's default branch. This recognizes operator adoption and explicit
replacement provenance without creating CI evidence or train receipts. It
rechecks the completion identity, repository, target and current checkpoint
source in the seal transaction. The same verified deliveries satisfy declared
epic dependencies, including prerequisites excluded from admission by a hold.
Changed sources, unproved completion generations, other targets and failed Git
observations supply no delivery proof. Holds, open gates and exact review rules
still control admission of any remaining candidate.

### Settle a parent whose children arrived through other routes

When every child is already delivered to the default branch, an obsolete
aggregate verifier can keep its managed parent PAUSED. A local operator or live
project supervisor can use `aq integration adopt <project> --task <parent>
--head-sha <current-main-sha> --settle-delivered-children --dry-run --reason '<why>'`
to observe the exact child completion sources. Omit `--dry-run` to cancel the
obsolete collection, retire detached delegates and record an audited operator
completion. Equivalent replacements also require `--accept-equivalent`.
The preview lists branch reservations it will release with their fence tokens.
A writerless `worker` reservation on a proven child's canonical branch is
released alongside the parent reservation in the same settlement transaction.
The recovery refuses pending or unknown child sources, retained
writers, manual holds, open gates and reconciler engine ownership. It records
`not_ci_attested` without adding CI or parent-verification evidence.

`aq doctor --check integration.delivered_children_unsettled_parent` reports this
state and its recovery command. See [the recovery steps and
blockers](integration-troubleshooting.md#all-children-reached-main-but-the-aggregate-verifier-is-stranded).

### Epic delivery in the dashboard

Epic cards and the task detail views show implementation progress
(`5/5 tasks complete`) apart from delivery. The delivery badge comes from a
read-only projection (`src/integration/epic_delivery.py`, design:
[epic delivery status](../superpowers/specs/2026-10-02-epic-delivery-status-design.md))
built on collection readiness, claim eligibility, live operations, branch
reservations and root receipts. *Integrating* and *Verifying* need a live
session with recent activity. *Delivered* needs a receipt binding the epic's
current head. *Paused* means an operator hold. A managed parent that integration
keeps `PAUSED` shows its delivery state instead, for example
`Integration blocked - final fix not collected` or
`Verification blocked - branch handoff required`. The badge names the blocker,
who has to act and when progress last happened. `get_task` returns the same
answer as `delivery_status`. Pause, resume and the integration controls still
act on the stored status.

### Candidate-member conflict repair

Batch formation inspects added Alembic migrations at the exact reviewed heads.
When two members reuse a revision ID or add sibling heads from the same parent,
the first ordered member rides the train and the others wait for a later sweep.
The supervisor receives a message naming the branches and declarations that need
rechaining. Rechain the deferred branch after delivery and review its new head.

`aq doctor --check integration.reviewed_file_guard` and `aq doctor --check
stall.sweep` report repairs blocked by a reviewed-file invariant. Once the repair
delegate has settled and detached, eject the conflicting member with
`aq integration eject`. For a recorded reviewed-file rejection, AQ verifies the
exact pushed SHA remotely and retains the rejected resolution. Other pending pushes
must first pass the existing resolution recovery command. AQ preserves its approval and
the old candidate evidence, invalidates the old CI result, and rebuilds a fresh
revision from the remaining sources. Attached writers, unresolved writes and
root promotion intents prevent ejection. An empty batch aborts and frees the sweep.
The agent-queue continuous policy in `docs/config/agent-queue-train-policy.json`
opts into root `admission: authorized`, `repair.source_ci: true`,
`repair.conflict_scope: batch` and `repair.on_exhausted: continue`. Completed
feature/bugfix tasks receive exact remote-head/tree authorization evidence tagged
with the project policy generation. `root.authorized_task_ids` explicitly admits
additional authorized task types; the agent-queue policy names the `steady-delta`
chore without changing its type or parent verification. It does not impersonate a human review or
override a rejection, open gate or hold. Editing that list swaps the policy
generation, which re-fences every pending source's authorization evidence and
source CI, so `configure` only accepts it while the train is drained. To deliver
one more explicitly user-authorized root while the train keeps running, record an
exact authorization instead:

```bash
aq integration authorize-root TASK_ID                                     # dry run
aq integration authorize-root TASK_ID --apply --head HEAD_SHA --reason "..."
```

The row in `integration_root_authorizations` pins the root's exact base, head and
checkpoint generation and stands in for the kind allowlist only. Policy,
generation, task type, active batches and their frozen snapshots are untouched.
Holds, open gates, a rejected review, `reviewed` admission and source CI still
bind, unrelated roots stay refused, and a moved head needs a new authorization.
Missing legacy root checkpoints still
require the audited `integration materialize-root` proof; branches without task
provenance remain outside admission.

Failed and terminally cancelled source checks file deduplicated repair roots with
exact source identity and actionable check links. A newer pending/successful
rerun supersedes an old cancellation. A PR GitHub reports as conflicting runs no
`pull_request` CI; with no required check on its head it is recorded as
`conflict`, and under `repair.conflict_scope: batch` it enters the train so the
batch repair resolves the conflict and the candidate's CI gates it. Repair branches preserve source ancestry;
when a repaired source is green, both it and its covered original source can
enter the train and receive normal delivery/cleanup receipts. Every final
candidate still requires its own exact authenticated green CI.

Before filing a repair, AQ asks canonical delivery truth the same way root
admission does (`src/integration/source_delivery.py`): the exact completion
generation, the exact repository and the exact default target ref, with git
proving the generation's retained source is contained there. A source already
delivered under other commits — merged, squashed, cherry-picked, or covered by
an `aq integration adopt --accept-equivalent` replacement — therefore files no
repair, and what was observed is recorded on the observation for an operator to
read. Only a proven answer withholds work: an unreachable repository, a missing
retained source, a new checkpoint generation, a reopened task, a different
target or a source that is genuinely not on the target all file the repair as
before.

Every eligibility decision observes afresh, and nothing persisted is read back
to decide one. Admission asks again on each poll, and a claim asks again before
it withholds a queued delegate — so a retargeted or rewound default branch, a
target that lost containment, and a delivery or adoption that arrives after an
earlier negative answer each release the repair again. A claim that cannot
reach git withholds nothing. The exact source identity is revalidated under the
hierarchy lock immediately before a proof is used, so a generation that moved
while git was read is a `stale` refusal rather than a withheld repair. An
already-filed delegate is never ended by this check, and a claimed one keeps its
writer.

With batch conflict scope, the assignment includes the whole frozen source
manifest. Start at its partial head, merge every remaining source in order, and
resolve all needed files in that one workspace. Earlier code, migrations and
generated files may change: re-chain migration collisions and regenerate
generated artifacts. Every frozen source must remain an ancestor. Record the
ordered **first-parent** range using
`git rev-list --first-parent --reverse PARTIAL_HEAD..HEAD`, then submit through
the same fenced `resolve-candidate-member` command below. AQ verifies the complete
batch ancestry and rejects unsealed side branches before accepting the aggregate.

Continuous repair stages retain finite time/attempt budgets. At exhaustion AQ
stops and proves the exact old writer, retains its checkout/index/dirty work,
fences and releases its pool claim, and files a fresh operation-bound stage.
Incomplete handoffs are retried by the reconciler, except for a delegate an
operator paused with `aq task pause`: it stays paused, even through an explicit
dispatch, until `aq task resume`. Old counters and history stay
visible. Periodic sweeps continue after the settling window is cleared, so a
released batch does not need another review event to schedule the next batch.

When a batch repair stage expires with unpublished work, its successor waits for
the old writer's confirmed stop. Dispatch then uses guarded owner recovery to
preserve committed or dirty progress at `aq/preserved/<owner-row-id>` before
releasing the old ownership. This targeted recovery does not wait for the optional
quiet-owner sweep. The successor's dossier records the exact tip, completed frozen
member merges, repair range, and recovery evidence; its checkout resumes that tip.
Changed preservation refs or frozen lineage block admission. Stage budgets and
the original candidate subject stay intact, and the resulting candidate still
requires exact CI before promotion.

For existing frozen policies using the default `conflict_scope: member` and
`on_exhausted: human`, the original protocol remains:

When ordered candidate construction conflicts on a reviewed member, the repair
delegate's task description records the exact batch, candidate revision, member
ordinal, partial head, and reviewed source range. Start from that partial head,
resolve only the named member, and commit a linear non-merge repair. Do not push
the integration branch directly. Instead, record the resolved head, tree, and
ordered commit range and let AQ perform the reserved remote mutation and guarded
handoff:

```bash
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
git rev-list --reverse PARTIAL_HEAD..HEAD
aq integration resolve-candidate-member \
  --resolved-head-sha RESOLVED_HEAD_SHA \
  --resolved-tree-sha RESOLVED_TREE_SHA \
  --repair-commit-sha REPAIR_COMMIT_SHA
```

Repeat `--repair-commit-sha` in the order printed by `git rev-list`. Inside a
pool session the command reads the claim epoch from `.aq/claim.json`. Batch,
revision, member, operation, workspace, partial head, and fence are deliberately
not command options: the daemon derives them from the authenticated live repair
assignment, verifies the exact remote resolution, and then continues with later
batch members under the collector's next fence.

A repair may change any file the candidate needs to merge and pass CI, including
reviewed code and files a reviewed member added. When an added Alembic migration's
revision ID collides with a sibling already in the partial candidate, assign it a
fresh revision ID and point `down_revision` (and the docstring's `Revision ID:` /
`Revises:` headers) at the partial candidate's single migration head, then check
`alembic heads`. Only the lineage stays exact: strict ancestry from the partial
head, the frozen ordered commit list, and no merge commits. A refused admission
includes `invariant` in both the initial and retry response; its reservation and
private repair ref remain forensic evidence.

## 5. Human controls and rollback

### The operator surface

The integration-train simplification (§5.1 of
[the review](../superpowers/specs/2026-10-02-integration-train-simplification-review.md))
narrows `aq integration` to the decisions a person makes and the reads that
explain them:

| Command | Kind | What it does |
| --- | --- | --- |
| `aq integration gate answer GATE_ID CHOICE` | decision | Answers an open integration gate with one of its own choices. Only the local operator's answer binds; a supervisor is refused with `verified_human_required` and escalates the gate. |
| `aq integration authorize TASK_ID` | decision | Authorizes a completed root's exact source (base, head, generation). A dry run by default; `--apply --head SHA --reason` records it. The old name `authorize-root` resolves to the same command. |
| `aq integration policy activate PROJECT_ID --mode M` | decision | Replaces `enable`, `develop` and `project set integration-policy`. With `--policy FILE` the policy is written under `--expected-generation`, then the mode changes under the generation that write produced. |
| `aq integration hold TARGET --reason R` | decision | Holds a subject's (or task's) integration until `--release`. The observer reports the hold and every engine mutation rechecks it, so nothing moves the held task. A root batch has no task of its own; hold a member. |
| `aq integration status PROJECT_ID [--subject ID]` | diagnostic | Adds `subjects`: each live reconciler subject with its blocker, wait reason, due time and open gate. |
| `aq integration explain TARGET` | diagnostic | The last recorded reconciler decisions for a subject, or for every subject a task belongs to, newest first. |
| `aq integration flush PROJECT_ID` | hand | Makes every live subject of the project due now, as well as the train sweep. |

The spec counts six surviving controls (four decisions, two diagnostics) but
keeps `flush` in its HAND row as "set `next_due_at = now`". The surface
therefore has seven: `flush` is kept as its own command and not folded into
`status`, which stays read-only.

Every other control is kept under `aq integration legacy …` until its removal
gate holds. Its old flat path (`aq integration enable`, `aq integration
resume`, …) still resolves, so existing runbooks keep working.
`src/commands/integration_legacy.py` records each legacy control's Appendix B
class, what replaces it and the condition for deleting it. Mechanical controls
go when every repository's subjects run on the reconciler engine and the
module behind them is deleted. Migration controls go when their doctor count
reads zero. `tests/test_integration_surface.py` fails when a command is
neither approved nor listed there, or when the supervisor's grants disagree
with the table.

Doctor follows the same split (§5.5):

| Check | Reports |
| --- | --- |
| `integration.subjects_overdue` | Reconciler-engine subjects past `next_due_at` by more than one visit interval, meaning nothing is visiting them. |
| `integration.subjects_held` | Subjects waiting on a person, oldest first, with how long each has waited: an open gate (`gate answer`) or an operator hold (`hold --release`). |
| `integration.trust` | The GitHub App installation and trust anchors (the probe formerly called `integration.app_mode`). |

The twenty older `integration.*` checks keep running until their removal gate
holds. Each is listed with its replacement in
`LEGACY_INTEGRATION_DOCTOR_CHECKS`.

### Operations, gates and cleanup

Status lists `repair`, `promotion`, `reconciliation`, and `cleanup_pending`
identities. Resume or abort only an operation already in `human_required`; both
fail closed when provider or irreversible-write facts are ambiguous. Abort is
database-only and does not rewrite Git. Cleanup retry requeues only the exact
safe existing items and never clears an irreversible marker:

```bash
aq integration resume integration-operation-id
aq integration abort integration-operation-id --reason 'operator chose forensic stop'
aq integration retry-cleanup integration-batch-id
```

Abort also frees the train's sweep request once nothing can still write for
the batch; otherwise every later flush would coalesce into a request no release
will ever end. `aq integration clear-stale-request PROJECT_ID` reports, as a dry
run, whether the outstanding request can still end, and `--apply --request-id
ID --reason REASON` frees a stale one. See [A train never
sweeps](integration-troubleshooting.md#a-train-never-sweeps).

A root enters the train only with a pull request and an approved GitHub review
of its exact head. An epic's PR opens when its parent verification completes;
a childless root filed onto its own `aq/epic/...` branch gets its PR when its
close records the finished head. The daemon retries a PR either path missed.
`aq integration redrive-root TASK_ID` reports, as a dry run, why a COMPLETED
root has no PR, and `--apply --head HEAD_SHA --reason REASON` opens it for
that head. See [A completed root has no pull
request](integration-troubleshooting.md#a-completed-root-has-no-pull-request).

The review poller (`src/integration/github_review_poll.py`) visits one page of
COMPLETED roots per tick and asks GitHub only what can have changed. It
re-reads a PR's reviews when the PR's `updated_at`, head or base changed, and
at least every ten minutes regardless; it does not fetch a PR it last saw
closed while that PR is absent from the repository's open-PR list, read once
per tick. A verdict or task authorization already stored for the exact source
identity is not proven against Git again. Source CI of an open exact PR is
still observed on every visit. The cache is in memory and keyed by the exact
source identity, so a restart or a new head re-observes the root in full, and
a failed read is retried on the next visit.

A collecting parent assembles a COMPLETED child only once approved evidence
pins the child's exact head; until then its siblings' `needs` keep them out of
the claim frontier. `aq doctor --check integration.stuck_children` lists
children still waiting after five minutes, and `aq integration redrive-child
CHILD_TASK_ID` says why one waits; `--apply --head HEAD_SHA --reason REASON`
records evidence for that head and queues the parent's collection. See [A
completed child is never assembled](integration-troubleshooting.md#a-completed-child-is-never-assembled).
If aggregate verification failed and `redrive-child` says the checkpoint is no
longer awaiting children, use `aq integration reopen-collection PARENT` to
diagnose recovery. It requires a settled failed verifier whose completion pins
the current remote checkpoint head, an additional completed child fix, detached
writers and no ambiguous mutation or manual hold. Apply with the reported full
`--head`, `--reason`, and `--apply`. The same episode and receipts survive; the
collector receives a fresh fence and the checkpoint advances generation. The
old failed verifier and repair budgets remain evidence. Redrive the child and
let a fresh verifier check the resulting exact head. Human rollout gates remain
binding; the old red aggregate is never certified by recovery.
For a legacy failure with an empty commit list, exactly one immutable
`task.integration_ready` outbox event must bind that verifier to the same parent,
episode, repository, branch and head before the failure. The dry run reports
the original subject in `delegates[].failure_subject`; apply includes that
binding and failed completion id in its audit event. The original completion
stays unchanged. Missing, contradictory or ambiguous evidence is refused.
A verifier session still attached to the branch must first be settled through
`aq session show SESSION_ID`, `aq session kill SESSION_ID` if still running,
and `aq integration release-owner --task-id VERIFIER_TASK_ID --dry-run`.
Apply owner recovery only after it proves the writer stopped and preserves its
work; then repeat the collection dry run. Worker tokens cannot perform those
operator steps. See the [failed aggregate recovery design](../superpowers/specs/2026-10-02-failed-aggregate-recovery-design.md)
for the calm-grove-25 handoff.

If `redrive-child` instead says the parent has no live collection operation,
`cancel-preserving` cancelled the parent's whole collection; `aq integration
reopen-collection PARENT_TASK_ID` reactivates it in its episode (receipts stay
bound) and gives the current conflict a fresh repair stage. See [A parent's
collection was cancelled](integration-troubleshooting.md#a-parents-collection-was-cancelled).
Sibling prerequisites accept a code receipt created after the child's latest
reopen; later task-row and close bookkeeping do not invalidate a delivered child.
`aq doctor --check tasks.ready_frontier_exclusions` lists READY tasks withheld
from the claim frontier and their reasons. `aq task explain --task-id TASK_ID`
evaluates the same claim filters for one READY task and names each failed
predicate with a `frontier_*` reason, including the hierarchy origin and
sibling-receipt checks, hold labels, and preparation backoff. Rework timestamps
on unrelated tasks do not affect a sibling's receipt. If a child completed again at the
same head after its receipt, `redrive-child` reports the stale receipt and
can reissue it after proving the incorporated head remains on the parent branch.

### Stopped pool-writer handoff recovery

For the current `agent-queue` incident, first inspect without changing state,
then resume the exact human-held repair operation. As of 2026-09-09 that
operation is `repair-batch-integration-batch-db9e3d6681c4b82624d569d2bdbf2a6e`:

```bash
aq integration status agent-queue
aq integration resume repair-batch-integration-batch-db9e3d6681c4b82624d569d2bdbf2a6e
aq integration status agent-queue
```

Run this only as the LOCAL operator, after the first status result still lists
that ID in `repair` with `state: human_required`. `resume` first checks the
durable handoff proof and releases only the exact stopped pool claim; it refuses
an active writer, a reused session/agent/slot, an operator hold, or any other
ambiguous shape. Do not clear claim, session, workspace, or owner rows manually.
For a later incident, copy the operation ID from the fresh status result rather
than reusing the example above.

For rollback, request disabled with the current generation:

```bash
aq integration status example
aq integration enable example --mode disabled --expected-generation 6 --reason 'roll back hierarchical integration'
aq integration status example
```

With no active work, disable is immediate. With frozen work, status shows the
managed effective mode, `desired_mode: disabled`, and `draining: true`; new
schedules and seals stop while existing work finishes safely. Poll status until
effective and desired mode are both disabled and `draining` is false. The
generation-locked drain completion restores the legacy routing policy recorded
at cutover; verify `legacy_suppression` in status. Do not delete history,
downgrade the database, or edit rollout columns to force rollback.

## Retired factory playbooks

`hierarchical-delivery`, `root-integration-train`, and
`memory-consolidation` were retired on 2026-09-11. They are no longer shipped,
seeded, or reviewed for import, and are not valid choices for a new project
policy. Do not import or activate them.

Frozen routes, artifacts, and run history remain evidence. A local operator
must use guarded integration commands to end or hold every referenced operation
before deleting the exact disabled catalog entries. Do not delete artifact
files, branches, or run records directly.

First inspect the affected project and playbooks:

```bash
aq integration status <project-id>
aq playbook activation-health --playbook-id hierarchical-delivery
aq playbook activation-health --playbook-id root-integration-train
aq playbook activation-health --playbook-id memory-consolidation
aq playbook list-runs --playbook-id hierarchical-delivery --status running
aq playbook list-runs --playbook-id root-integration-train --status running
aq playbook list-runs --playbook-id memory-consolidation --status running
```

For each operation still using a retired route, use the existing guarded
integration disposition chosen from fresh status; for example,
`aq integration abort <operation-id> --reason 'retire obsolete integration playbook'`.
The command must preserve live ownership, branches, and historical evidence.

Only after every reference is ended or visibly held and the activation is
disabled, delete the exact catalog entries as the local operator:

```bash
aq playbook delete --playbook-id hierarchical-delivery --scope system --scope-identifier '' --artifact-sha256 579b8a1a92b66d885acba6417483e5426f2bab6691c55bd8810ee3bd087a4d82
aq playbook delete --playbook-id root-integration-train --scope system --scope-identifier '' --artifact-sha256 a93c3c32e05bdff64f25279531b6eaf04aba01e8bbd48a8125230a1ca32fa160
aq playbook delete --playbook-id memory-consolidation --scope system --scope-identifier '' --artifact-sha256 536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166
```

The delete command itself refuses active or referenced work. That refusal is a
safety result: retain the evidence and resolve the named operation rather than
forcing deletion.

Before deleting `memory-consolidation`, verify the pre-existing exact backup:

```bash
backup=$(mktemp -d)
tar -xzf ~/.agent-queue/vault/backups/policy-retirement-2026-09-11/memory-consolidation-exact.tar.gz -C "$backup"
sha256sum "$backup/vault/system/playbooks/memory-consolidation.md" "$backup/compiled/artifacts/536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166.json"
```

The required hashes are respectively
`808ee5daafed43bc936d6a43be6132e2fa65f161ba0395ce6dfaa44590d06b69` and
`536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166`.

## Release limits

- GitHub.com is the only supported forge for this rollout.
- Security/protection inspection, positive/negative scratch probes,
  transport/worker/control-plane isolation certification, and the broad
  crash/recovery/PostgreSQL race matrix are deferred. Status reports
  `certification.status: not_performed`; it never claims certification.
- Existing authenticated runtime boundaries, exact CI/OID validation, and
  irreversible-write safeguards remain enforced. A YAML claim cannot replace
  them.
- Prevent workers from reaching the tokenless privileged LOCAL operator API by
  deployment isolation (loopback binding plus an OS/container boundary).
  Managed writers must use fresh daemon-issued, session-instance-bound
  `AQ_API_TOKEN` values; restart them after changing the authentication
  boundary. A session token cannot mint another session's token or invoke the
  LOCAL-only rollout controls. Do not set
  `api_auth.require_session_token: true` for this rollout unless the deployment
  also provides a separately reviewed LOCAL operator access path: the current
  CLI has no global operator-token flow, so that setting by itself rejects the
  unauthenticated LOCAL CLI needed for these controls.
- Same-UID, unconfined stock deployments have no isolation certification in
  this release. As an operator risk recommendation, keep them disabled or in
  observe until an independent deployment boundary is established; this is not
  a functional preflight certification gate in the accepted operational scope.
- This guide performs no production deployment or enablement. Each mutation is
  an explicit LOCAL-operator action and must be evaluated against a fresh
  status result.
