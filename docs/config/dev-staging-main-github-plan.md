# Dev, staging and main activation

This is the reviewed repository preparation for the 2026-10-07 takeover.
The [JSON plan](dev-staging-main-github-plan.json) records the read-only GitHub
snapshot and the exact proposed API payloads. Preparing it makes no GitHub changes.

## Branch flow

| Branch | Entry | Required check from App 5075923 |
|---|---|---|
| `dev` | Root integration of authorized source work | `Agent Queue Integration Attestation` |
| `staging` | Continuous promotion step `staging` from `dev` | `Agent Queue Promotion Attestation (staging)` |
| `main` | Requested promotion step `release` from `staging` | `Agent Queue Promotion Attestation (release)` |

The staging step has approval, versioning and release notes set to `none`.
The release step requires operator approval, uses `semver_tag` from `pyproject.toml`,
creates `v{version}`, reads `src/releases/notes/{version}.md`, and backmerges after
promotion. Both steps inherit the required check set. These are the promotion-flow
settings installed with the release implementation; GitHub branch rules enforce
their distinct attestations.

The audit workflow runs on pushes to all three branches and binds the attestation
name, step and exact `refs/heads/...` target. Full CI runs on candidate refs and on
pull requests into all three branches. An attested branch push does not rerun CI;
an unverifiable push invokes the audit fallback.

## Observed state

GitHub currently has only `main`, at
`259c0f56ecf9a9d9e3c0a18d31df9b67264eea3f`, and uses it as the default branch.
Ruleset `21960669` prevents deletion and non-fast-forward updates on `main`.
Ruleset `24002443` requires the legacy integration attestation on `main`.
Neither ruleset has bypass actors. The attestation App variable is already
`5075923`; the check-version variable is still `ci-4c6e0c2a989c`, while the
reviewed policy and manifest require **`ci-4f7c710bba01`**.

## Apply after the final takeover commit lands

1. Confirm the final main SHA contains the workflow, verifier and trust manifest.
   Read that SHA immediately before creating branches. Substitute it for
   `${LANDED_MAIN_SHA}` in the two branch-creation payloads; do not use the older
   observed SHA. If a branch already exists, inspect it and reuse its current head
   only after confirming the required repository content is present.
2. Correct `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` to `ci-4f7c710bba01`.
   Keep `AQ_INTEGRATION_ATTESTATION_APP_ID` at `5075923`.
3. Create `dev` and `staging` at the landed SHA. Expand basic deletion and
   non-fast-forward protection to all three branches.
4. Create the dev and staging attestation rulesets. Replace ruleset `24002443`
   with the release attestation rule in the JSON plan: leaving its old required
   check would require two incompatible delivery proofs on `main`.
5. Set GitHub's default branch to `dev`. Bind AQ's repository default branch and
   project integration target to `dev`, and install the project-scoped promotion
   flow and enabled `promotion-continuous` / `promotion-request` activations.
6. Read back the branch heads, default branch, variables and each ruleset. Confirm
   the required checks are pinned to App `5075923`, bypass actors are empty, and
   basic protection covers all three branches. AQ must observe the same branch,
   step names and check-version values before it resumes delivery.

The API payloads contain no pull-request requirement or strict base-up-to-date
check: the integration service fast-forwards an exact tested and attested SHA.
The operator controls the release request and approval before main promotion.

For rollback, stop delivery before changing repository rules. Restore the two
existing rulesets and default branch from `observed` in the JSON plan. Newly
created dev/staging protection rulesets should be removed only after delivery is
stopped. Preserve branch history and the recorded activation state.
