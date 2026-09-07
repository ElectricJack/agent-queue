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

## 2. Configure the exact GitHub App and repository trust

The daemon configuration contains identity and an absolute key-file reference,
never PEM or token bytes:

```yaml
integration:
  default_mode: pull_request
  merge_ci_policy: required
  merge_required_checks:
    - Tests (default)
  github_app:
    client_id: Iv1.exampleclientid
    app_id: 123456
    installation_id: 23456789
    private_key_path: /run/secrets/aq-integration-app.pem
```

Edit this with `aq system config set` or replace the section with
`aq system update-config --section integration --data JSON`. Validate first:

```bash
aq system config set 'integration.github_app={client_id: Iv1.exampleclientid, app_id: 123456, installation_id: 23456789, private_key_path: /run/secrets/aq-integration-app.pem}' --dry-run
aq system config set 'integration.github_app={client_id: Iv1.exampleclientid, app_id: 123456, installation_id: 23456789, private_key_path: /run/secrets/aq-integration-app.pem}'
aq restart
```

The App installation must be limited to the designated GitHub.com repository
and grant the repository permissions the integration runtime already requires,
including **Variables: read**. The daemon requests `variables:read` on its
exact-repository installation token and rejects a token response that omits it.
The CLI does not grant or mutate GitHub App permissions; an App owner must
change the installation grant, then restart the daemon for credential/config
changes.

Commit `.github/agent-queue-integration.json` on the default branch. Start from
`.github/agent-queue-integration.example.json`; set the AQ repository config
ID, GitHub numeric repository ID/full name, attestation App ID, CI producer App
ID, and the exact check-set name/version. The parent and root policy
`producer_id` values must match that CI producer. Set the two repository
Actions variables with operator credentials:

```bash
gh variable set AQ_INTEGRATION_ATTESTATION_APP_ID --repo OWNER/REPOSITORY --body 123456
gh variable set AQ_INTEGRATION_REQUIRED_CHECK_VERSION --repo OWNER/REPOSITORY --body checks-v1
```

These values must match the default-branch trust manifest and the root policy.
Functional preflight reads all three through the repository-bound App client.

## 3. Bind reviewed project artifacts, classes, and profiles

Prepare reviewed V2 bundles whose compiled scope is `project` and whose scope
identifier is the target project. Importing never activates:

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
are explicit; nothing is inferred at enable time.

```json
{
  "version": 1,
  "parent": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "15368"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep", "debug_profile_id": "worker-deep-high-claude"},
    "route": {"playbook_id": "hierarchical-delivery", "scope": "project", "scope_identifier": "example", "activation_id": null, "artifact": {"playbook_id": "hierarchical-delivery", "artifact_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "schema_generation": 2, "contract_fingerprint": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "source_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
    "primary_intelligence_class": "standard",
    "primary_profile_id": "worker-standard-medium-claude",
    "verifier_intelligence_class": "standard",
    "verifier_profile_id": "worker-standard-medium-claude"
  },
  "root": {
    "required_checks": {"version": "checks-v1", "names": ["Tests (default)"], "producer_id": "15368"},
    "repair": {"primary_seconds": 1800, "primary_attempts": 3, "debug_seconds": 3600, "debug_attempts": 3, "debug_intelligence_class": "deep", "debug_profile_id": "worker-deep-high-claude"},
    "route": {"playbook_id": "root-integration-train", "scope": "project", "scope_identifier": "example", "activation_id": null, "artifact": {"playbook_id": "root-integration-train", "artifact_sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "schema_generation": 2, "contract_fingerprint": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "source_digest": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", "compiler_build": "playbook-v2-compiler/1", "compiled_at": "2026-09-06T00:00:00Z", "version": 1}},
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
aq project set example integration-repository-id repo --expected-integration-generation 0 --reason 'bind exact GitHub repository'
aq integration status example
POLICY_JSON="$(jq -c . /srv/aq/reviewed/example-integration-policy.json)"
aq project set example integration-policy "$POLICY_JSON" --expected-integration-generation 1 --reason 'bind reviewed routes and policy'
aq integration status example
```

Do not put rollout mode fields through `aq project set`; mode changes exist
only under `aq integration enable`.

## 4. Roll out one mode at a time

First enter observe and clear every functional blocker shown by status. Observe
runs eligibility without scheduling or mutating Git:

```bash
aq integration enable example --mode observe --expected-generation 2 --reason 'begin observation'
aq integration flush example
aq integration status example
```

If status reports only `legacy_pr_merge_gate` blockers, an operator may make
that exact history inapplicable. Copy the current digest once, create the
immutable waiver, then pass the returned waiver ID to the cutover:

```bash
aq integration waive-history example --reason 'accept reviewed pre-cutover history' --blocker-digest sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
aq integration enable example --mode hierarchy --expected-generation 3 --reason 'enable recursive delivery' --waiver-id WAIVER_ID
```

Otherwise do not waive: fix the repository, policy, artifact, activation,
profile, intelligence-class, trust-manifest, hosted-variable, or runtime-wiring
blocker. In hierarchy mode, terminal children integrate children-first and the
parent is completed only after exact current-generation/head receipt and parent
verification. Root train sweeps remain off.

After observing recursive delivery, advance to train using the fresh generation:

```bash
aq integration status example
aq integration enable example --mode train --expected-generation 4 --reason 'enable root train sweeps'
aq integration flush example
aq integration status example
```

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
