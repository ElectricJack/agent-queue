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
   hand-merge hole too. Set `strict_required_status_checks_policy: true`
   on it (see the next section for why and for the payload).

Doing step 3 before step 1 trades one silent failure mode for a loud one.
Doing step 2 first is cheap and reversible.

## The second class: green on a stale base (#390 + #391)

### What happened

Later on 2026-09-03, with the gate above already shipped as `warn`, `main`
went red again on the very same test — and this time **both PRs were green
on their own CI**.

- PR #390 (`aq/solid-harbor.68`, merged 20:36) committed the
  ruff-formatted `packages/aq-client/README.md`.
- PR #391 (`aq/solid-harbor.71`, merged 20:40) was branched from
  `384da566`, seven commits before #390 landed, and added explicit
  `post_hooks` in `scripts/openapi-python-client.yaml` that scope ruff to
  `agent_queue_api_client/` — so the generator never produces that README.

Each PR's `pull_request` run tested its head merged into the base *as the
base was when the run started*. Nothing ever tested #390's README next to
#391's hooks. The merge did, and combined `main` failed
`test_generated_client_boilerplate_matches_what_the_pinned_generator_writes`
(recorded digest `8ce684…`, fresh generation `95156c…`). PR #395 regenerates
the README; it fixes the artifact, not the class.

The class is generic: **any pair of PRs where one commits a generated
artifact and the other changes how that artifact is generated** — or, more
broadly, any two changes whose combination is not the sum of their parts.
The rollup gate cannot see it, because every rollup it reads is green.

A second, smaller gap made it worse. `.github/workflows/tests.yml` runs on
every push to `main`, so the merge commit *is* tested post-merge — but the
workflow's `concurrency` group was keyed by ref, and GitHub keeps at most
one **pending** run per group. Three merges in forty seconds (#381, #391,
#392) cancelled the runs for #381 and #391 outright; #391's merge commit was
never tested on `main` at all, and the red only surfaced on #392's run,
which also carried #392's own unrelated failures.

### Options evaluated

| Option | Blocks pre-merge? | Cost | Verdict |
|---|---|---|---|
| (a) "Require branches to be up to date" on `main` | Yes | Every merge on a busy queue re-runs CI after an update-branch; ~12 min serial per merge in a burst | **Fleet-path equivalent shipped now; the ruleset flag is step 3 above** |
| (b) Post-merge run on `main` that cannot be lost | No — reports within one cycle | One runner per merge commit instead of one per burst | **Shipped** |
| (c) Merge queue | Yes, and tests PRs *together* | Needs a required check (so needs (a)'s prerequisites), `merge_group` trigger, and `gh pr merge --auto` semantics in `pr_merge` | Trigger declared; queue itself deferred |

(a) as a repo setting has the same prerequisite as the required check
itself — a green `main` — and one more: `pr_merge` had no way to recover
from "branch is out of date", so flipping the flag would have refused every
fleet merge with no path forward. (b) is free of that prerequisite and was
needed regardless: a `main` run that gets cancelled is a `main` run that
never happened.

### What shipped

**1. `integration.merge_require_up_to_date` (default `true`).** `pr_merge`
now asks a second question alongside the rollup: is the head up to date
with its base? `GitManager.apr_behind_base` compares the head against the
base tip (`repos/<o>/<r>/compare/<base>...<head>`, GitHub's own
`behind_by`), `src/git/ci_gate.py`'s `classify_base` judges it
(`current` / `stale` / `unknown`), and the result lands in the `ci` block
as `base`:

```json
"ci": {
  "policy": "required",
  "state": "green",
  "base": {"ref": "main", "behind_by": 7, "state": "stale"},
  "blocked": true,
  "message": "https://github.com/o/r/pull/391: head is 7 commit(s) behind main (stale). Refusing to merge under integration.merge_ci_policy: required. fix the failing checks, wait for the run to finish, update the branch so its checks re-run against the current base (gh api -X PUT repos/o/r/pulls/391/update-branch), or pass force=true."
}
```

The policy semantics are unchanged: under `warn` a stale base merges and
is reported; under `required` it refuses, `unknown` refuses (fail closed,
same as an unreadable rollup), and `force` overrides and is recorded. It is
its own knob because it is the expensive half of `required`: on a busy
queue every merge invalidates every other open PR, and the recovery is an
update-branch plus a full CI cycle. An operator who wants `required` without
the serialisation sets `merge_require_up_to_date: false` and accepts this
class of break as a post-merge report.

Recovery from a refusal is the same thing GitHub would demand:

```bash
gh api -X PUT repos/<owner>/<repo>/pulls/<n>/update-branch   # gh ≥ 2.55: gh pr update-branch
# wait for the new run, then merge again
```

**2. `main` runs are keyed by commit.** `.github/workflows/tests.yml`'s
concurrency group is now `tests-<ref>-<sha>` on `main` (and still
`tests-<ref>-head` on branches, so a newer push keeps superseding its own
stale checks). Every merge commit gets its own run that nothing cancels, so
a combination that only exists on `main` is reported on the commit that
created it, within one CI cycle, with no bisecting. This is what makes
"reported on `main` within one CI cycle" true rather than merely likely; a
scheduled run was considered and rejected because it adds nothing a
per-commit push run does not already give.

**3. `merge_group` trigger declared.** The workflow now also runs on
`merge_group` events, which is the one workflow-side prerequisite for a
merge queue. It is inert until an operator enables the queue.

### What is deliberately not done yet

The ruleset is unchanged — still `deletion` + `non_fast_forward`, no
required check. Adding the required check with the strict flag is step 3
of the sequencing above and needs a green `main` first. When that day
comes, this is the payload (the `strict_required_status_checks_policy`
flag is option (a)):

```bash
gh api -X PUT repos/<owner>/<repo>/rulesets/<id> --input - <<'JSON'
{
  "name": "Basic Protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_status_checks",
     "parameters": {
       "strict_required_status_checks_policy": true,
       "required_status_checks": [
         {"context": "Tests (default)"},
         {"context": "Tests (migration-and-slow)"},
         {"context": "Tests (postgres-integration)"}
       ]}}
  ]
}
JSON
```

With `merge_require_up_to_date` already on in the fleet, flipping the flag
changes nothing about what the daemon merges — it only closes the
hand-merge hole, exactly as the required check does for red rollups. A
merge queue (option (c)) is the step after that if the re-run cost of
strictness turns out to matter: it tests each PR against `main` plus the
PRs queued ahead of it, so the combination *is* tested pre-merge and
nothing has to be re-run by hand.
