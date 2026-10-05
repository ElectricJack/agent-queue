# Agent Queue train policy handoff

The project-scoped bundles in `src/prompts/reviewed_playbooks/` are compiled
against the current V2 command and event registries. The parent route handles
the current `integration_complete_parent` outcomes, including idempotent
`already_completed` and terminal `failed`. The policy JSON binds their exact
artifact identities and the sixteen `Tests (...)`, `E2E CLI (...)` and
`Dashboard (typecheck/build)` checks from
`.github/workflows/tests.yml` to the producer `"15368"`, GitHub Actions' numeric
App id, on both boundaries.

The numeric id is the canonical producer in both credential modes, and it is
what `aq integration onboard-train` emits in either one. GitHub reports it as
`check_run.app.id`, and the App-mode trust manifest names it as
`ci_producer_app_id`, so the same policy binds under existing-login or App
credentials without a rebind. The slug `github-actions` is legacy. A policy
that already holds it keeps working under existing-login credentials, because
frozen snapshots and CI evidence rows record it, but App credential mode
refuses it with the preflight blocker `ci_producer_not_numeric`. The earlier
revision of this file used the slug, so an installed agent-queue policy bound
from it must be rebound to this JSON before App mode.

The `ci-4f7c710bba01` check set (the planner's digest of the names) adds
`Dashboard (typecheck/build)` to the previous fifteen-check set
`ci-4c6e0c2a989c`. It requires all eight `Tests (default-N/8)` shards, all four
`E2E CLI (...)` scenario groups, and a dashboard that typechecks and builds.
The operator must approve this check-set version bump before a supervisor or
operator rebinds the installed policy to this JSON and updates the
`AQ_INTEGRATION_REQUIRED_CHECK_VERSION` Actions variable. Regenerate the App
trust manifest alongside the policy; replace any explicit legacy merge
requirement or GitHub branch rule with the complete required set. Existing
operations retain their frozen policy snapshots. A worker's workflow change
does not update an installed project policy. See
[App-mode check-set rotation](app-mode-train.md#95-changing-the-required-check-set-rotation).

The bundles are copied to `vault/reviewed-playbooks/<id>/` when the deployed
daemon seeds reviewed bundles. Only a project-scoped supervisor or local
operator should run the following commands. Review the manifest and source
first. Do not change the project's development mode until the approved cutover
prerequisites allow a drain; that mode is the current publisher's validation
gate. Use the fresh integration generation for every mutation.

```bash
aq playbook v2-validate --path reviewed-playbooks/agent-queue-parent-integration/artifact.json
aq playbook v2-validate --path reviewed-playbooks/agent-queue-root-train/artifact.json
aq playbook v2-import --path reviewed-playbooks/agent-queue-parent-integration
aq playbook v2-import --path reviewed-playbooks/agent-queue-root-train
aq playbook activate --playbook-id agent-queue-parent-integration --artifact-sha256 sha256:4f3bf46a93c26507e7920deb93cf3b97cbe5caa6d8ad834fd0dec30d00825122 --enabled
aq playbook activate --playbook-id agent-queue-root-train --artifact-sha256 sha256:579813fdfdfd2978115f513cb8c8cb1690c362a4f78099fb0e1d371a033d5a9d --enabled
aq playbook activation-health --playbook-id agent-queue-parent-integration
aq playbook activation-health --playbook-id agent-queue-root-train
```

Both validations should have zero errors or questions. Imports must report the
two hashes above. Activation health must report each exact hash as enabled and
ready. A stale contract or activation refusal is a blocker; do not change the
policy snapshot to conceal it.

After the development publisher is safely drained and the project can enter
disabled mode, bind the repository, review mode, and policy in order. The
project-scoped supervisor runs these itself: `aq project set` admits the three
integration keys from a live, named supervisor of the project, the same gate as
every operator integration control, and its shipped profile already grants
`edit_project`. It also grants `integration_adopt_legacy_deliveries`. Shipped
profiles are write-if-absent, so a vault supervisor profile seeded earlier
lacks that grant. On such an install the operator adds it once with
`aq agent profile-reseed --profile-id supervisor --grants-only`. The
repository ID `agent-queue2` comes from the existing development deliveries;
verify that its stored GitHub origin and default branch are exact before
binding it. The helper reads a fresh generation for each command, avoiding a
stale CAS value.

```bash
generation() {
  aq --json integration status agent-queue |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["generation"])'
}

aq integration status agent-queue
aq integration enable agent-queue --mode disabled --expected-generation "$(generation)" --reason 'drain development publisher for train policy binding'
aq integration status agent-queue
aq project set agent-queue integration-repository-id agent-queue2 --expected-integration-generation "$(generation)" --reason 'bind exact existing repository'
aq project set agent-queue integration-review-mode pull_request --expected-integration-generation "$(generation)" --reason 'require human PR review'
policy_json="$(python3 -c 'import json; print(json.dumps(json.load(open("docs/config/agent-queue-train-policy.json")), separators=(",", ":")))')"
aq project set agent-queue integration-policy "$policy_json" --expected-integration-generation "$(generation)" --reason 'bind reviewed train policy and artifacts'
aq integration enable agent-queue --mode observe --expected-generation "$(generation)" --reason 'preflight train configuration without scheduling'
aq integration flush agent-queue
aq integration status agent-queue
aq integration adopt-legacy-deliveries --project-id agent-queue --dry-run
aq integration adopt-legacy-deliveries --project-id agent-queue
aq integration status agent-queue
```

Parents that finished before the train have no parent collection and never
will, so their children can never get train receipts. The same holds for a
finished parent whose collection was cancelled when the project switched to
development (`noble-ridge` and `sound-current` keep such checkpoints). Status
already accepts a child the development publisher delivered to `main` (its
delivery row is the receipt). Every other terminal child of a finished parent
is reported as `missing_receipt` with cause `no_parent_collection` until
`adopt-legacy-deliveries` proves it: a development delivery's commit or the
child's branch tip is an ancestor of `origin/main`, or merging a delivery
commit, the branch tip or the latest completion commit into `main` changes
nothing (the work landed under other commits: `content_equivalent`). Review the
dry run first; the real run records one audited row per proven child and is
safe to repeat. It lists every child it cannot prove with the reason:

- `not_on_default_branch`: no delivered commit is on `main`, and merging the
  work would still change it. `undelivered` shows the commit examined and what
  it would still add (diffstat and paths, or a conflict); it is `null` when no
  commit could be examined.
- `child_not_completed`: a failed child has nothing to deliver.
- `parent_not_terminal`: the parent is still open, so its completion needs
  real train receipts. Nothing can adopt these children.

Only after the human decides, settle a named child with a reason:
`--supersede TASK_ID --by SHA --reason '...'` when SHA on `main` re-delivered
the work, `--retire TASK_ID --reason '...'` when the work was abandoned
(nothing is deleted), or `--accept TASK_ID --reason '...'`. A read-only
diagnosis on 2026-09-24 (tasks `fair-horizon`, `noble-stone`) found every
other legacy child covered by a development delivery to `main`, including the
17 `noble-ridge.*` and `sound-current.*` children once their cancelled
collections are recognised. It expects four children to be listed as
`not_on_default_branch` for a human decision:

- the repairs `development-repair-1f49a87816f28bf5ae4c` (under `sharp-crest`,
  tip `58d162c2`, still adds 5 files, +143 lines) and
  `development-repair-ac45f2a85d942d50cf37` (under `sharp-journey`, tip
  `a67d8fae`, 4 files, +292/-24): only their parent-collection deliveries were
  published, and their `main` deliveries stayed parked. Their work is not on
  `main`: deliver it, or retire it;
- `clear-meadow.5`: no branch on origin and no delivery; its completion commit
  `d42e7a43` would still add 7 files (+76). Retire it if abandoned;
- `smart-stone.3`: re-delivered by `fresh-quest` (`ac89ac04e`) except 3 files
  (+26 lines: a gate note and two test additions). Supersede it by that merge
  if the remainder is not owed.

In observe mode `repository_not_designated` names each task in `ref`, with a
`cause`. On 2026-09-24 it named 96 terminal hierarchy tasks with
`task_repository_unset` (graphs created with `aq task create --graph` in
development mode are not bound to `agent-queue2`) and any task created while
the project was in observe mode, which binds no repository either. A live task
must be bound or finished before cutover. Nothing can yet settle the terminal
ones: task `swift-pinnacle` asks for that decision, and `bold-cascade` fixes the
creation paths that leave the repository unset.

This install configures `integration.github_app`, so agent-queue's train runs
in App credential mode, the production configuration. Its functional preflight
also needs `.github/agent-queue-integration.json` (committed, and pinned to this
policy by `tests/test_integration_trust_manifest.py`), the two
`AQ_INTEGRATION_*` Actions variables, and a `main` ruleset that requires the
integration attestation pinned to the App with no bypass; the numeric producer
above is already the one App mode requires. The App-mode cutover interleaves
those anchors with the commands above (the variables and the ruleset go between
the drain and the bind), with the output to expect at each step:
[app-mode-train.md §9.3](app-mode-train.md#93-cutover-supervisor-with-the-repository-admin-at-the-marked-steps).
Every other project's cutover is in [train-onboarding.md](train-onboarding.md).

The status response in observe mode must report zero functional blockers and
`ready: true`; activation health above confirms the exact active route hashes.
Observe does not schedule train batches. The later train-mode cutover belongs to the supervisor after
scratch proof and a green `main`; this handoff does not authorize it.
