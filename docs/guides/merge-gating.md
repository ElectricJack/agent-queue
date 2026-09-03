# Merge gating — don't let red CI land on `main`

## What happened

On 2026-09-03, PR #341 ("docs(client): sync README with pinned generator")
merged into `main` with this rollup:

```
Tests (default)              FAILURE
Tests (default)              CANCELLED
Tests (migration-and-slow)   SUCCESS  ×2
Tests (postgres-integration) SUCCESS  ×2
```

Its single commit `7eba1124` replaced `packages/aq-client/README.md` with
the pre-pin generator's bytes — exactly the regression
`tests/test_api_client_contract.py::test_generated_client_boilerplate_matches_what_the_pinned_generator_writes`
exists to catch. **CI caught it. The merge happened anyway.**

Nothing was bypassed. Two things were simply never true:

1. **`main` has no required status check.** `gh api
   repos/<owner>/<repo>/branches/main/protection` answers `404 Branch not
   protected`. The only ruleset on `main` ("Basic Protection") carries
   `deletion` and `non_fast_forward` — nothing about checks. GitHub was
   never asked to block a red merge.
2. **The fleet's merge path never looked at CI.** `pr_merge` went straight
   to `gh pr merge --squash --delete-branch`, which only refuses when
   branch protection refuses.

A survey of PRs #324–#353 found **29 of the last 30 merges red on `Tests
(default)`**. #341 was not an outlier; it was the norm. The reason is
compounding: `main` itself is red, so every branch cut from it inherits the
failure and "this PR is red" stops carrying information about *this PR*.

## The two halves of the fix

### 1. `integration.merge_ci_policy` (in this repo, shipped)

`pr_merge` now reads the PR head's status-check rollup before merging and
applies a policy. See `src/git/ci_gate.py` for the judgement and
`GitCommandsMixin._check_ci_before_merge` for the application.

| Value | Behaviour |
|---|---|
| `off` | Never asks. The pre-2026-09-03 behaviour. |
| `warn` | **Shipped default.** Asks, merges regardless, and returns the verdict in the command result's `ci` block and the daemon log. |
| `required` | Refuses to merge anything that is not green — including a rollup that cannot be read (fail closed). |

```yaml
integration:
  merge_ci_policy: required
  # Empty means every check in the rollup, which is the strict reading.
  # Name checks explicitly when some arm of the matrix is advisory.
  merge_required_checks:
    - Tests (default)
```

`warn` is the shipped default rather than `required` on purpose: turning
`required` on while `main` is red stops **every** merge in the fleet, which
is a decision for an operator to make deliberately, not a side effect of
upgrading. `warn` costs one `gh pr view` per merge, changes nothing about
what lands, and makes the verdict visible to the final-reviewer and to
anyone reading the log.

A human who has looked at a failure and judged it unrelated can override a
`required` refusal with `force`:

```bash
aq git pr-merge --project-id <pid> --pr-url <url> --force
```

The override is recorded as `ci.forced` in the result and logged. It is not
something a worker profile can reach — `pr_merge` stays whitelisted to
`final-reviewer` only.

### 2. Branch protection on `main` (operator decision, **not yet done**)

The config knob only governs merges the daemon performs. A human running
`gh pr merge` by hand, or clicking Merge in the GitHub UI, is still
unguarded. The durable fix is a required status check:

```bash
gh api -X PUT repos/<owner>/<repo>/rulesets/<id> \
  -f 'rules[][type=required_status_checks]' ...
# or, in the UI: Settings → Rules → Basic Protection → add
# "Require status checks to pass" → "Tests (default)"
```

**Do not enable this until `main` is green.** As of 2026-09-03 the latest
completed run on `main` (`33747391898`, commit `ab6b7dd5`) is `Tests
(default) failure` with the other two matrix arms green, and the same shape
holds several commits back. A required check on a red `main` blocks every
PR in the fleet at once.

The sequencing that works:

1. Get `Tests (default)` green on `main`.
2. Flip `integration.merge_ci_policy` to `required` and watch one merge
   cycle — this catches regressions at the fleet's own merge path, where
   the error message reaches the agent that tried to merge.
3. Add the required status check to the ruleset, which closes the
   hand-merge hole too.

Doing step 3 before step 1 trades one silent failure mode for a loud one.
Doing step 2 first is cheap and reversible.
