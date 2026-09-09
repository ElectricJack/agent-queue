# Hierarchical integration trains: operator rollout

This rollout is per project and defaults to disabled. It upgrades an existing
SQLite installation on SQLite, or an existing PostgreSQL installation on
PostgreSQL. It does not deploy, enable, or change GitHub configuration by
itself.

The command synopsis used below is:

```text
aq integration status PROJECT_ID
aq integration flush PROJECT_ID
aq integration enable PROJECT_ID --mode observe --expected-generation GENERATION --reason REASON
aq integration enable PROJECT_ID --mode train --interval-seconds SECONDS --expected-generation GENERATION --reason REASON
aq integration reconcile-unmaterialized PROJECT_ID --expected-generation GENERATION --reason REASON
aq integration waive-history PROJECT_ID --reason REASON --blocker-digest BLOCKER_DIGEST
aq integration resume OPERATION_ID
aq integration abort OPERATION_ID --reason REASON
aq integration retry-cleanup BATCH_ID
aq project set PROJECT_ID integration-repository-id REPOSITORY_ID --expected-integration-generation GENERATION --reason REASON
aq project set PROJECT_ID integration-policy POLICY_JSON --expected-integration-generation GENERATION --reason REASON
```

Always take `GENERATION` and, for a history waiver, `BLOCKER_DIGEST` from a
fresh `aq integration status` result. A stale result is returned as stale; the
CLI never rereads and retries a mutation against a newer generation.

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

Do not change database backends during this release. Both
`src/database/migrate_sqlite_to_pg.py` and `scripts/migrate_sqlite_to_pg.py`
omit integration state. Pointing a populated SQLite installation at
PostgreSQL after using either copier is unsupported and would lose that state.
This limitation does not affect an in-place schema upgrade on the backend the
installation already uses.

## 2. Use the daemon user's existing GitHub login

A GitHub App is **not required**. AQ defaults to `gh api` for repository,
PR, and CI reads/writes, and existing authenticated Git transport for exact
fetch/push operations. Run these as the same OS user that runs the daemon:

```bash
gh auth status --hostname github.com
git ls-remote https://github.com/OWNER/REPOSITORY.git HEAD
```

If the account is not logged in, run `gh auth login --hostname github.com`.
The account needs write access to the project repositories and permission to
read their CI results. AQ does not print or persist a copy of the gh token.
GitHub repository protections remain enforced; AQ does not bypass them.

The ordinary integration configuration needs no App IDs or private key:

```yaml
integration:
  default_mode: pull_request
  merge_ci_policy: required
  merge_required_checks:
    - Tests (default)
```

The parent and root policy below declare the exact CI check names, version,
and producer (`github-actions` for GitHub Actions, or the real numeric producer
App ID). This identifies who ran CI, not a separate AQ App you must register.
AQ verifies the repository identity and exact commit against authenticated
GitHub results, then records durable CI receipts before guarded promotion.
No `.github/agent-queue-integration.json`, AQ attestation App, or
`AQ_INTEGRATION_*` Actions variables are needed in the default gh mode.

Existing installations that explicitly configure `integration.github_app`
retain the legacy App-authenticated path and its trust manifest/variables.
Remove that configuration to use the default gh path, then restart the daemon.
Historical internal names containing `app_client` are compatibility names,
not a requirement to configure an App.

The repository's CI workflow must run on the exact pushed parent and generated
integration branch commits, not only on `main` or a pull request's synthetic
merge commit. Declare every full-CI job required at each boundary. The AQ
repository ships exact-candidate CI reuse on promotion to `main`; other
repositories need equivalent workflow support before train enablement if they
must avoid a second full CI run after promotion. Do not waive missing CI or
substitute an empty required-check set.

## 3. Bind reviewed shared artifacts, classes, and profiles

The default `hierarchical-delivery` and `root-integration-train` playbooks are
shared system-scoped V2 bundles. Import and activate each once, then reference
the same exact artifact from each project's policy with `scope: system` and
an empty `scope_identifier`. Schedules, repositories, CI requirements, repair
budgets, and operation state remain per project. Importing never activates:

```bash
aq playbook v2-import --path /srv/aq/reviewed/hierarchical-delivery
aq playbook v2-import --path /srv/aq/reviewed/root-integration-train
aq playbook activate --playbook-id hierarchical-delivery --artifact-sha256 sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --enabled
aq playbook activate --playbook-id root-integration-train --artifact-sha256 sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --enabled
```

Use the full hashes returned by import, not the sample hashes above. Confirm
the exact active artifacts and every referenced route before configuration:

```bash
aq playbook artifacts --playbook-id hierarchical-delivery
aq playbook artifacts --playbook-id root-integration-train
aq system list-intelligence-classes
aq agent get-profile --profile-id worker-standard-medium-claude
aq agent get-profile --profile-id worker-deep-high-claude
```

The policy is one JSON object. This example is structurally valid; replace its
sample hashes, identities, project ID, activation identities, classes, profiles,
and checks with the exact imported and installed values. Parent and root routes
are explicit; nothing is inferred at enable time. A project-specific override
may instead name a project-scoped artifact for that exact project. Other
project identities and agent/supervisor scopes are rejected. System activation
does not enable integration for projects that have no policy or remain disabled.

```json
{
  "version": 1,
  "parent": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "github-actions"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep", "debug_profile_id": "worker-deep-high-claude"},
    "route": {"playbook_id": "hierarchical-delivery", "scope": "system", "scope_identifier": "", "activation_id": null, "artifact": {"playbook_id": "hierarchical-delivery", "artifact_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "schema_generation": 2, "contract_fingerprint": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "source_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
    "primary_intelligence_class": "standard",
    "primary_profile_id": "worker-standard-medium-claude",
    "verifier_intelligence_class": "standard",
    "verifier_profile_id": "worker-standard-medium-claude"
  },
  "root": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "github-actions"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep", "debug_profile_id": "worker-deep-high-claude"},
    "route": {"playbook_id": "root-integration-train", "scope": "system", "scope_identifier": "", "activation_id": null, "artifact": {"playbook_id": "root-integration-train", "artifact_sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "schema_generation": 2, "contract_fingerprint": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "source_digest": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
    "primary_intelligence_class": "standard",
    "primary_profile_id": "worker-standard-medium-claude",
    "verifier_intelligence_class": "standard",
    "verifier_profile_id": "worker-standard-medium-claude"
  },
  "branchless_parent": "verifier",
  "on_failed_child": "block",
  "on_main_moved": "rebuild",
  "cleanup": {"max_attempts": 5, "retry_base_seconds": 30.0, "retry_max_seconds": 3600.0, "successful_source_refs": "delete", "failed_work_retention_seconds": 604800}
}
```

Repository and policy changes are accepted only while the project is disabled,
fully drained, and has no active integration work. Bind one field, reread status
for the incremented generation, then bind the next:

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
Repository, review-mode, and policy changes all require LOCAL operator authority
and the fresh integration generation while disabled and drained.

Do not put rollout mode fields through `aq project set`; mode changes exist
only under `aq integration enable`.

## 4. Roll out one mode at a time

Keep `default-pipeline` enabled: its per-task reviews supply the exact approval
evidence trains need, and its spec/proposal rules remain in use. Hierarchy/train
mode suppresses only its legacy per-branch final-review/merge route. Retire the
project's `pr-merge-sweep` activation after cutover; keep the template available
for projects using legacy delivery. `ci-main-sentinel` remains a read-only
fallback observer of existing main CI and files repair PRs through the train.
`blocked-task-escalation` must defer integration-owned tasks to operation-level
recovery instead of generic task recovery or replacement repair budgets.

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

If status reports only `legacy_pr_merge_gate` blockers, an operator may make
that exact history inapplicable. Copy the current digest once, create the
immutable waiver, then pass the returned waiver ID to the cutover:

```bash
aq integration waive-history example --reason 'accept reviewed pre-cutover history' --blocker-digest sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
aq integration enable example --mode hierarchy --expected-generation 4 --reason 'enable recursive delivery' --waiver-id WAIVER_ID
```

Otherwise do not waive: fix the repository, policy, artifact, activation,
profile, intelligence-class, GitHub authentication, CI-policy, or runtime-wiring
blocker (manifest/hosted-variable checks apply only to legacy App mode).
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
project cannot have overlapping active trains. Every nonempty batch uses an
ephemeral integration branch, including a singleton batch. Main promotion is
permitted only for the exact candidate OID already proven by the configured CI
producer; there is no post-main audit run. Ordinary task PRs retain the full-CI
fallback.

Successful integration/source branches are deleted by the default cleanup
policy. Failed forensic work is retained for `604800` seconds by default.

### Candidate-member conflict repair

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

## 5. Human controls and rollback

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
