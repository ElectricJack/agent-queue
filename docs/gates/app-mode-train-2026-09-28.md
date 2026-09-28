# App-mode integration train proof: 2026-09-28

The live proof in App-mode spec §10 (vault
`projects/agent-queue/specs/2026-09-27-app-mode-integration-train-production-github-app-credentials.md`,
review `rev-swift-glacier`), task `noble-impact-73.9`. Driven by
`scripts/e2e-app-train.sh` and `scripts/e2e/app_train.py`. Every id and SHA
below is copied from the run's evidence ledger
(`~/.agent-queue-app-train/evidence.jsonl`). Every GitHub payload the run read is
kept under `~/.agent-queue-app-train/payloads/`. The fixture repository and the
scratch database are both kept for inspection.

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

<!-- Filled from the evidence ledger when the live run completes. -->
