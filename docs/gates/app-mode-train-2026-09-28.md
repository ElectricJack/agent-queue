# App-mode integration train proof: 2026-09-28

The live proof in App-mode spec §10 (vault
`projects/agent-queue/specs/2026-09-27-app-mode-integration-train-production-github-app-credentials.md`,
review `rev-swift-glacier`), task `noble-impact-73.9`. Driven by
`scripts/e2e-app-train.sh` and `scripts/e2e/app_train.py`. Every id and SHA
below is copied from the run's evidence ledger
(`~/.agent-queue-app-train/evidence.jsonl`). Every GitHub payload the run read is
kept under `~/.agent-queue-app-train/payloads/`. The fixture repository and the
scratch database are both kept for inspection.

## Result

S1 to S10 all ran live, and every scenario passed.

- The attested promotion was accepted with no bypass (S4, S5, S8).
- Both S6 negative controls were rejected by the ruleset.
- The S8 rotation used no GitHub bypass at any step.
- The S10 break-glass push ran the audit's fallback CI.
- The protection reader's `current_user_can_bypass` assumption (spec §8.3) is
  confirmed live. The App's own installation token read `never` without a
  bypass and `always` with the App as a bypass actor. GitHub withheld the
  `bypass_actors` list from that token.

One manual intervention was needed. The S8 switch's drain to `disabled` never
finished, because of a defect, `noble-impact-73.11`. The run released the
provably terminal branch-owner rows in the scratch database to let it finish.
Nothing on GitHub was involved. See [Manual interventions](#manual-interventions).

## Approval

- **Request:** the worker messaged `user:dashboard` (`msg-85e328fc…`, follow-up
  `msg-ac5e6382…`) with every planned fixture mutation, before the first one.
- **Approval:** `system:supervisor` answered in `msg-be54c5e6d5064099b3df805f28531c03`
  at 08:58 UTC. It relayed that Jack had approved the App-mode spec, which
  includes this proof, and that the fixture repository is his.
- **Limits it set:** mutate only the fixture (repository `1384141153`, with its
  variables, manifest, workflows, rulesets and PRs); mint tokens for the
  fixture only; record every mutation.
- **Reading applied:**
  - Every App installation token was minted for repository `1384141153` alone.
  - The admin-only writes spec §10 gives the operator used the operator's `gh`
    login and targeted only the fixture. Those writes are the variables, the
    ruleset, the setup pushes, the negative controls and the PR approvals.
  - Nothing touched `ElectricJack/agent-queue` or any other repository.

## Isolation and identity

- **Source:** the daemon ran from worktree commit `3874fca3e`, the tip of the
  implementation epic `noble-impact-73` (`.1`-`.8`). The harness commits touch
  only `scripts/` and `tests/fixtures/`.
- **Fixture:** `ElectricJack/aq-gh615-app-fixture-20260923`, a private
  repository with id `1384141153`. Recorded base `main` is
  `72ce5b2d100ca1d44758a7bb824b803692d6d779`. It is recorded, not
  force-reset.
- **App:** the production `agent-queue-train` (App `5075923`, installation
  `164874645`), with the key file referenced in place from the operator's config.
  Every token was minted for repository `1384141153` alone
  (`AppTokenProvider.mint`).
- **Daemon:**
  - It listened on `127.0.0.1:8157`, used disposable PostgreSQL container
    `aq-app-train-pg` (`postgres:18-alpine`, `127.0.0.1:5557`, database
    `aq_app_train_e2e`), kept its home at `~/.agent-queue-app-train`, and ran the
    fake session provider. The harness played every pool worker.
  - Its process environment, read from `/proc/<pid>/environ`, had no `GH_TOKEN`,
    `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` or `SSH_AUTH_SOCK`, and no worker
    session variable. `GH_CONFIG_DIR` was an empty directory, and global and
    system Git config were `/dev/null`. `gh auth status` under that environment
    exited 1 before start.
- **Operator identity:** the harness process alone used the operator's `gh`
  login (`ElectricJack`, repository admin) for the anchors only an admin may
  write and for the negative controls.
- **Scratch project:** `aqapp-0928`, onboarded by `github_clone` through the
  App. Its integration repository id `aqapp-0928` is the manifest's
  `canonical_repository_id`. The shared routes are `parent-integration`
  `sha256:65f97002…3bf51` and `root-train` `sha256:5808faf0…4f2fe`, both
  imported and active.

## Prepare (read-only on GitHub)

| Read | Result |
| --- | --- |
| Actions variables (operator) | `AQ_INTEGRATION_ATTESTATION_APP_ID=5052310` (the deleted test App), `AQ_INTEGRATION_REQUIRED_CHECK_VERSION=fixture-v1` |
| Protection, read as the App | `rules/branches/main`: HTTP 200, no rules. Classic protection: HTTP 200, requiring `fixture` from App 15368, `strict: true`. This recording replaced the hand-built `tests/fixtures/github_protection/fixture-classic-protection.json`, which had `strict: false`. |
| `onboard-train` (App mode, shared routes) | policy `fixture-v1` / `["fixture"]` / producer `"15368"`; manifest naming App `5075923`; audit workflow with the verifier inlined and `unattested-ci` calling `ci.yml`; the §8.1 ruleset |
| `app-verify --policy` | `credential`, `repository`, `producer` ok. `manifest` fail `trust_manifest_mismatch`: the committed manifest names App 5052310 and canonical id `train-fixture`. `variables` fail `hosted_workflow_variables_mismatch`. `protection` fail `main_protection_missing`. `audit_workflow` warn `audit_workflow_missing`. |
| Epic graph dry run | leaf plus a sibling `reviewer` node with `discovered-from`: accepted |

## Scenarios

All times are UTC on 2026-09-28. "Operator" means the harness process using the
operator's `gh` login.

| # | Result | Key evidence |
| --- | --- | --- |
| S1 | pass | Setup commit `99f7e228`; `app-verify` ok on every item except `protection`, which failed `main_protection_missing` |
| S2 | pass | Ruleset `24108467`: `attested_only` with `current_user_can_bypass` `never`, then `app_bypass` with `always`, then back to `never` |
| S3 | pass | `hosted_workflow_variables_unavailable`, `ci_producer_not_numeric` and `trust_manifest_mismatch`; each restored |
| S4 | pass | Candidate `df8c833b` promoted with no bypass. Attestation `108871530825` by App 5075923. The audit reported attested, and `unattested-ci` was skipped. |
| S5 | pass | Revision 0 `95c87d78` red. Revision 1 `63a80b3f` was adopted automatically, attested (`108875590945`) and promoted. |
| S6 | pass | (a) `1631ed1d` and (b) `195192b4` both rejected with GH013. `select_trusted_attestation` refused the forgery. |
| S7 | pass | `subject_trust_invalid` names parent `wise-quest`, generation 1, head `ede9b209`, cause `missing` |
| S8 | pass, with one intervention | Expand promoted under `fixture-v1`. The switch moved the variable to `fixture-v2`. Contract attestation `108887821880` carries `fixture-v2`. No bypass at any step. |
| S9 | pass | `develop` refused with `main_protection_blocks_development_publisher` under `attested_only`; accepted with the App bypass; disabled again |
| S10 | pass | Break-glass `3ead00bc`. The audit reported NOT attested, and `unattested-ci / fixture (v2)` succeeded. |

### S1: setup (anchors)

| Step | Evidence |
| --- | --- |
| Classic protection removed (operator) | `DELETE /repos/…/branches/main/protection` at 09:25:27. Afterwards the App reads HTTP 404. |
| `trust-manifest --write` (daemon, App) | Wrote the fixture manifest, sha256 `dfba47ea…a842`: canonical id `aqapp-0928`, repository `1384141153`, CI producer `15368`, attestation App `5075923`, `fixture-v1` / `["fixture"]`. Byte-identical to `onboard-train --write-trust-manifest`. |
| Setup commit (operator push, no ruleset yet) | `72ce5b2d` → `99f7e2283d606436def43f60aabd3636934ad199`. It adds the manifest and the rendered `main-attestation.yml` (verifier inlined, `unattested-ci` calls `ci.yml`). `ci.yml` now pushes only on `aq/parent/**` and `aq/integration/**` and declares `workflow_call`. |
| `app-setup --apply` (operator `gh`) | Set only the differing variable, `AQ_INTEGRATION_ATTESTATION_APP_ID` `5052310` → `5075923`. `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` `fixture-v1` was left alone. The re-verify showed `variables` ok. |
| `app-verify --policy` | `credential`, `repository`, `producer`, `manifest`, `variables` and `audit_workflow` ok; `protection` fail `main_protection_missing` (unprotected) |
| Audit on the setup commit | Run `36403412778`. The variable still named App 5052310 when the push landed, so the verifier reported `NOT attested (no … check run from App 5052310 …)` and `unattested-ci / fixture` (job `108866617898`) ran green. |

### S3: preflight negatives (before the ruleset)

| Negative | Evidence |
| --- | --- |
| (a) Variable deleted | The operator deleted `AQ_INTEGRATION_REQUIRED_CHECK_VERSION`. `app-verify` reported `variables` fail `hosted_workflow_variables_unavailable`. `app-setup --apply` recreated it, and `variables` was ok again. |
| (b) Slug producer | Policy with `producer_id: github-actions`. `app-verify` reported `producer` fail `ci_producer_not_numeric`, with `manifest` still ok, so it was not `trust_manifest_mismatch`. Bound and flushed in `observe`, the status blockers included `ci_producer_not_numeric`. Mode then went back to `disabled` and the numeric policy was rebound. |
| (c) Wrong attestation App in the manifest | Operator pushed `633a190f782be6650d2da6caccef91b4613dd7f6` (`attestation_app_id` 5052310). `app-verify` reported `manifest` fail `trust_manifest_mismatch`. Restore commit `6bab464a71d8ed4f87b117cf6517360100561865`; `manifest` ok. |

### S2 and S9: protection reader and development guard

| Step | Evidence |
| --- | --- |
| §8.1 ruleset applied (operator `POST`) | Ruleset `24108467` "Train-only main" requires `Agent Queue Integration Attestation` with `integration_id` 5075923, and has `bypass_actors: []`. Updated `09:28:48`. |
| Read as the App | `rules/branches/main`: one `required_status_checks` rule from ruleset `24108467`. `rulesets/24108467`: `current_user_can_bypass: "never"`, no `bypass_actors` field. Classic protection 404. `app-verify` reported `protection` ok (`attested_only`) and `ready: true`. |
| S9a | `aq integration develop aqapp-0928 --validation none` was refused: `main_protection_blocks_development_publisher: … default branch protection is attested_only …` |
| App bypass added (operator `PUT`, `09:29:01`) | `{"actor_id": 5075923, "actor_type": "Integration", "bypass_mode": "always"}`. Read as the App: `current_user_can_bypass: "always"`, still no `bypass_actors` field. `app-verify` reported `protection` warn `main_protection_app_bypass`. |
| S9b | `develop` was accepted (generation 5 → 6), then `enable --mode disabled` (→ 7) |
| Bypass removed (operator `PUT`, `09:29:29`) | `bypass_actors: []`. Read as the App: `never`, `attested_only`. The ruleset stayed exactly like this until S10. |

### Train mode

The v1 policy was bound with pull-request review. `observe` was `ready` with
zero blockers and zero warnings (generation 10). Then `hierarchy`, then `train`
with a 60 s cadence (generation 12). The harness played every pool worker. The
reviewer's no-code receipt used the supported local-operator
`aq integration record-noop`. After each operator-account approval the harness
ran `aq integration flush`, which skips the fixed five-minute settling window.

### S4: green path

| Boundary | Evidence |
| --- | --- |
| Leaf | `sound-current.1` pushed `aq/sound-current.1` at `40da29dd2b1d507d999817be462aa2865b7540d7` through the App; closed `shipped`. |
| Reviewer | `sound-current.2` closed no-op. Review evidence `review-07665026-9428-5ffc-812f-9e7c7804c9c0` pins the leaf head. No-op receipt `9412a1d0-b492-45a0-ae20-f186030f0433`. |
| Parent collection | Receipt `receipt-4190f955-0005-5052-8390-2d6c10887c24` squashed the leaf onto `aq/epic/app-train-s4-023158-add-train-s4-txt` at `737258a1a17f43d4ac50f2e812ba29773372c128`. |
| Parent CI and verification | The daemon published `aq/parent/sound-current/c5e278ff80ba672891c402d4a839d085/1/737258a1…`. Verification `50853810-01e3-4809-af09-a3d2cad18aba` recorded `737258a1` (verifier operation `3bd1404c…`). |
| Epic PR | [PR #9](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/9) opened at 09:35:05 with a one-file diff (`train-s4.txt`). The operator account approved exact head `737258a1` in review `5336674364`, a test action and not an independent review. |
| Seal and candidate | Batch `integration-batch-8d087c8963022f418af01dc9e9053f91`. Candidate `df8c833bb61a82cddc6b9cf9c3661278265dd0fc` on `aq/integration/p-0d0f4d1e480b4a7a9d297a69e8f5e8ad/r-6a48ea2167e2c7b77cf50bbc9d4c7537`. Push CI run `36404978189`, check `108871423828`, success. |
| Attestation | Check run `108871530825` by App 5075923, suite `98570638218`. `output.text` is canonical, and `external_id` is `aq-attestation-v1:4252490e…176b`, its sha256. Version `fixture-v1`, checks `["fixture"]`. |
| Promotion | GitHub accepted the push: `main` = `df8c833b` = `tested_candidate_sha` = `final_main_sha`. The ruleset at promotion was unchanged since 09:29:29, with `bypass_actors: []`. PR #9 and audit [PR #10](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/10) show merged at 09:40:49. Cleanup `complete`; released `1790588449.74`. |
| Audit | `Main attestation` run `36405033926`: "Main attestation: attested (attested by check run 108871530825 from App 5075923; 1 checks re-verified)." `unattested-ci` (check `108871643917`) was skipped. |

### S5: red, then in-place repair, then green

| Boundary | Evidence |
| --- | --- |
| Leaf and review | `sound-forge.1` deleted `train-repaired.txt` at `9f02001828b20065446c714532a6a9dcf87d3b5b`. `sound-forge.2` closed no-op, receipt `ccbdc758-abda-4897-9c13-0c9d1ee522cf`. [PR #11](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/11) head `3a94bcc5…`, approved in operator review `5336827491`. |
| Revision 0 red | Batch `integration-batch-494199612ad17a05168fcf31d3f78ccf`, candidate `95c87d78b8533131307b44346a9322c96ae390f2`. Push run `36406146014`, check `108875244310`: **failure**. A later `pull_request` check on the same SHA (`108875257578`) succeeded and was correctly ignored, because the observer counts push runs only. The 09-24 false-green stays fixed. |
| Repair | `repair-batch-integration-batch-494199612ad17a05168fcf31d3f78ccf`, stage 0 on `train-worker`. Delegate `repair-repair-batch-…-0` restored the file, pushed `63a80b3f2b9e740cf6ef1af1d846a8092800aa82` on the same integration branch, and closed `shipped`. |
| Revision 1 | Adopted automatically: the `root-train` rule `continue-closed-root-repair` fired with no replay, unlike 09-24. Push run `36406222327` success. Attestation `108875590945` by App 5075923 (`fixture-v1`). Promoted with no bypass: `main` = `63a80b3f`. PR #11 and audit PR #12 merged 09:53:01. `Main attestation` run `36406288864` reported attested, and `unattested-ci` was skipped. |

### S6: negative controls

| Control | Evidence |
| --- | --- |
| (a) Unattested push | The operator pushed `1631ed1df0ee08923ff33862c02096dc7be08b4a` to `main`: `GH013 … Required status check "Agent Queue Integration Attestation" is expected.` It was rejected and `main` stayed `63a80b3f`. |
| (b) Forged attestation | Scratch branch `aq-app-train/forged-attestation` at `195192b4dc281e3e6b765b1c249215b291f8825d`. Workflow run `36406354238` created check run `108875944348`, named `Agent Queue Integration Attestation`, conclusion success, attributed to App **15368**. The operator's push of that SHA to `main` was rejected: `GH013 … Required status check "Agent Queue Integration Attestation" was not set by the expected GitHub app.` |
| Daemon selection | `select_trusted_attestation` over the live check runs of `195192b4`, read as the App, raised `trusted attestation is missing`. The scratch branch was deleted afterwards. |

### S7: subject trust visibility

Epic `wise-quest`: leaf `wise-quest.1` deleted
`.github/agent-queue-integration.json` at `c6598cc5f4fbd529c5a3d52e6ea2d2b238546b24`.
Reviewer receipt `db45fe18-8ab4-4bff-bf8c-77eb5a1d814f`. Once the parent
collected, `aq integration status` showed this blocker:

```json
{"code": "subject_trust_invalid", "target_kind": "parent",
 "subject": {"parent_task_id": "wise-quest", "generation": 1},
 "head_sha": "ede9b2092c4df072301f5157c8472e8ead4c0b3d", "cause": "missing", "fields": [],
 "detail": "parent wise-quest generation 1 at ede9b209…: subject tree has no .github/agent-queue-integration.json; refresh the branch from the default branch"}
```

The parent is left in that state for inspection. It never reached a PR.

### S8: rotation (expand, switch, contract)

The rotation renamed the job `fixture` to `fixture (v2)`. The ruleset was not
edited at any step, from `09:29:29` until S10, and no bypass actor existed.

| Step | Evidence |
| --- | --- |
| Render | `onboard-train` rendered the v2 policy (`fixture-v2` / `["fixture (v2)"]`, both boundaries) and the manifest from an expand tree. The audit workflow is unchanged. |
| Expand (under v1) | Epic `azure-flare`. Leaf `azure-flare.1` (`882851cae2928740822138b0d9b5ed47ffd0ad3e`) changed `ci.yml` to run both jobs and the manifest to `fixture-v2`; it was pushed through the App's `workflows: write`. The parent tree's manifest names another check set than the v1 snapshot and was accepted on identity (spec §5.2). [PR #13](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/13), review `5336930367`. Batch `integration-batch-af4f5e9707097e6cc3d1c9db13dc463a`, candidate `62982f4693eaeb0fb13df778e0f3e4a618415756`. Attestation `108877745971` with version **`fixture-v1`**, checks `["fixture"]`. Promoted with no bypass; `Main attestation` run `36406933112` reported attested. Status then carried the non-blocking warning `trust_manifest_check_set_differs`. |
| Switch | `enable --mode disabled` did not finish draining (see [Manual interventions](#manual-interventions)). After the drain (generation 14): bound the v2 policy; `app-setup --policy v2 --apply` set `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` `fixture-v1` → `fixture-v2`; `observe` `ready` with zero blockers and **zero warnings** (generation 17); `hierarchy`; `train` (generation 19). |
| Contract (under v2) | Epic `clear-flare`. Leaf `clear-flare.1` (`9cd1fa365398a7ec47b8593ba1cf666857ca60dc`) removed the old job. Parent CI passed on `fixture (v2)`. [PR #15](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/15), review `5337254959`. Batch `integration-batch-092556892ec7a0beaac2bd9588a9db3f`, candidate `cc79fa1dc9a8bf8273ea3046f9dc20f3ed9fa719`. Attestation `108887821880` with version **`fixture-v2`**, checks `["fixture (v2)"]`. Promoted with no bypass; `Main attestation` run `36410059540` reported attested against the updated variable. |

### S10: break-glass

The operator added `{"actor_id": 5, "actor_type": "RepositoryRole",
"bypass_mode": "always"}` (the admin role) at `10:30:40`. The operator then
pushed `3ead00bc55670afbf17d931dcc90784fc810b1d4`, and GitHub accepted it:
"Bypassed rule violations for refs/heads/main: Required status check …". The
bypass was removed at `10:30:45`, back to `bypass_actors: []`. `Main
attestation` run `36410146825` reported: "Main attestation: NOT attested (no
Agent Queue Integration Attestation check run from App 5075923 on 3ead00bc…);
unattested-ci runs the full suite." Job `unattested-ci / fixture (v2)`
(`108888210800`) succeeded. The ruleset's history on GitHub holds both edits.

## Manual interventions

- **S8 switch: releasing branch owners in the scratch database.**
  - `enable --mode disabled` left the project `draining` for more than 10
    minutes. At that point there was no active batch, no member, no pending
    cleanup and no reconciliation.
  - `IntegrationControls._has_active_work_on` (`src/integration/controls.py`)
    counts every `integration_branch_owners` row that is not `released`. Twelve
    rows stayed `reserved` or `handoff_pending` after their work was terminal:
    - the collector row of each released batch;
    - the verifier row of each completed epic;
    - the worker row of each finished child.
  - Both supported release commands refused every row:
    - `release-stale-owners`: `owner_blocked`, "the hierarchy recovery path
      owns this row";
    - `release-owner`: `not_recoverable_state`, or `writer_live` for the one
      `handoff_pending` reviewer row.
  - The script `~/.agent-queue-app-train/release_owners.py` released exactly
    those 12 rows, with the values `finished_owners.py` writes, as a
    compare-and-swap on the fence. It did so only after checking each row's
    work was terminal:
    - collector rows: the batch is `promoted` with cleanup `complete`;
    - verifier rows: the epic is `COMPLETED`;
    - worker rows: the task is `COMPLETED`.
  - The drain then finished within one tick, which confirms the cause.
  - Filed as `noble-impact-73.11`. Another worker has since fixed it
    (`aq/noble-impact-73.11`, `c0ee21b79`): the drain and status predicate
    ignore terminal reservations, and stopped-writer recovery runs while
    draining. That fix landed after this run and is **not** re-proven live here.
- **Operator-account approvals** of PRs #9, #11, #13 and #15 are test actions,
  not independent reviews, as in the 09-24 run.
- **`aq integration flush`** after each approval skipped the fixed five-minute
  settling window, a supported command.

## Findings and follow-ups

| Finding | Disposition |
| --- | --- |
| A train project can never drain to `disabled` after promoting a batch, because terminal branch-owner reservations are never released | `noble-impact-73.11`, fixed after the run (`c0ee21b79`) |
| `aq task create --graph --dry-run` accepts a node `task_type` (`review`) that the real create rejects | filed as `noble-impact-73.12` |
| `aq wait register --kind message` without `--after-seq` fails with a raw pydantic error, and a worker cannot wait on the reply to its own message | filed as `noble-impact-73.13` |
| The fake session provider's reviewer session stayed `draining` after close, keeping its claim when the branch handoff could not be proven; that left one `handoff_pending` row. This is the 09-24 stop-proof gap, in the test provider only. | recorded here; 73.11's recovery-while-draining change covers the release side |
| Spec §10 S9 says `enable --mode development`, but that mode is `aq integration develop` (`enable` accepts `disabled`, `observe`, `hierarchy`, `train`) | documentation only; the runbook already uses `develop` |

Confirmed working live for the first time:
- automatic revision-1 continuation after a repair close (`smart-stone.5`);
- verifier-free parent completion;
- `record-noop` as the reviewer's receipt route;
- the push-only CI observer against a green `pull_request` check;
- a workflow change pushed and promoted through the App's `workflows: write`.

## Fixture state after the run

- **`main`:** `3ead00bc55670afbf17d931dcc90784fc810b1d4`, the S10 break-glass
  commit on top of `cc79fa1d`.
- **Protection:** ruleset `24108467` is active with no bypass. Classic
  protection is removed.
- **Variables:** `5075923` and `fixture-v2`.
- **Workflows:** `ci.yml` runs only `fixture (v2)`. The manifest names
  `fixture-v2`, and `main-attestation.yml` is in place.
- **Leftovers:** the S7 parent `wise-quest` and its branches, and task and
  parent branches from the batches.
- **Scratch world:** the database (container `aq-app-train-pg`) and home are
  kept. The disposable daemon is stopped.

## Offline coverage from the recordings

- **`tests/fixtures/github_protection/`:**
  - the fixture's classic protection, as the App read it (`strict: true`;
    this replaced the hand-built copy, which had `false`);
  - the §8.1 rules, and ruleset `24108467` with `never` and `always`;
  - the classic-protection 404.

  `tests/test_integration_protection.py` replays them through
  `protection.read_protection` (`attested_only` and `app_bypass`) and pins the
  §8.3 assumption.
- **`tests/fixtures/hosted_attestation/`:**
  - the S4 (`fixture-v1`) and S8-contract (`fixture-v2`) attestations, with
    the CI check runs they name;
  - the S6b forgery.

  `tests/test_hosted_attestation.py` shows three things:
  - the stdlib verifier accepts both live attestations byte for byte, and they
    parse as `AttestationPayload`;
  - it refuses S4's under `fixture-v2`;
  - it refuses the forgery, which does not come from App 5075923.
- **`tests/fixtures/app_mode/`:** both Actions variables as GitHub returned
  them after the switch. `tests/test_integration_app_mode.py` reads them as ok
  under the switched policy and as a mismatch under another.
