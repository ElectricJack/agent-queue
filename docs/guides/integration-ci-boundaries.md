# CI at integration boundaries

This repository's [Tests workflow](../../.github/workflows/tests.yml) runs on:

- Pull requests into `dev`, `staging` and `main` (opened, synchronized,
  reopened or marked ready for review).
- Pushes to `aq/integration/**`, `aq/batches/**`, `aq/parent/**`,
  `aq/promote/**` and `aq/backmerge/**`.
- Explicit `workflow_dispatch` and reusable `workflow_call` invocations.

Delivery-branch pushes and feature-branch pushes without a PR do not directly
trigger that workflow. The [delivery audit](../../.github/workflows/main-attestation.yml)
observes pushes to `dev`, `staging` and `main`; when App attestation variables
are configured and trusted branch attestation is absent, it invokes Tests as a
reusable fallback. With those variables unset, no fallback CI runs. Those checks use the prefix
`unattested-ci /`. A committed `promotion-source-audit` check set can authorize
that evidence for promotion source admission; candidate checks and publication
attestations retain their canonical names. See [promotion flows](promotion-flow.md).

A newer push cancels obsolete Tests work on the same ref. Draft PRs wait until
ready for review. A same-repository PR from `aq/integration/**` skips its own
run because the branch's push already tests that head. The workflow matrix is
the current suite inventory; it is no longer the historical four-arm layout.

Manual feature testing is available through:

```bash
gh workflow run tests.yml --ref YOUR_FEATURE_BRANCH
```

A manual run is useful evidence for a person, but cannot stand in for a required
push-event observation. Required jobs are evaluated individually on the exact
candidate; an unrelated workflow failure does not make a successful required
job fail. Missing, cancelled and unavailable evidence never becomes a pass.
See [required-job settlement](../specs/design/required-job-train-settlement.md).

The [candidate status workflow](../../.github/workflows/train-candidate.yml)
posts `aq-train/candidate` after a successful Tests push run on
`aq/integration/**` or `aq/batches/**`. The Git-first train independently
requires its configured exact checks, reviews, branch lease and attestation
before an expected-old fast-forward. A check run on a PR merge commit does not
prove a different batch candidate passed.

Epic checks are advisory by default. Only an enabled project playbook's
`tables.parent_episode.required_checks` makes them a gate. This does not relax
root or promotion checks. The retained parent verification adapter can publish
an immutable `aq/parent/<task>/<operation-hash>/<generation>/<sha>` snapshot
for historical parent operations; that adapter does not start a Subject runtime.
Current epic assembly and root delivery run through the train.

The [CI main sentinel](../../src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md)
observes check evidence on the configured head; a commit with no exact evidence
remains pending. It does not treat a successful worker close as CI proof.

## Rollout to existing branches

GitHub reads push workflow definitions from the pushed revision. A workflow
change on the default branch does not update older candidate trees. Merge the
reviewed workflow change into a retained candidate through its authorized writer
when necessary, then test the changed exact SHA. Replaying an unchanged ref or
manually dispatching Tests cannot fabricate the required push run.

Branch names do not change this rule: existing task branches remain valid, and
`aq/feature/<task>` is a convention for manually created feature branches.
The actual trigger list and trusted producer policy decide which CI counts.
