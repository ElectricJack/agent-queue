# Train onboarding for every project

Jack (2026-09-27): every AQ project should deliver through the integration
train. This runbook covers the other nine active projects. agent-queue keeps
its own reviewed policy and handoff,
[agent-queue-train-policy.md](agent-queue-train-policy.md).

Two pieces make any project bindable:

- **Shared routes.** The reviewed, system-scoped bundles `parent-integration`
  and `root-train` (`src/prompts/reviewed_playbooks/`). Import and activate
  them once per install; every project's policy can then name them.
- **The planner.** `aq integration onboard-train PROJECT` reads the project's
  workflows from Git, derives the required check set, writes the policy, and
  prints every command from the project's current state to a ready
  observe-mode train. It changes nothing.

The mode flips are operator-scoped. A LOCAL operator, or a live named
supervisor of the project (or the global supervisor), runs every command
below. A worker's token is refused at scope.

## Where each project stands

The table reflects the state read on 2026-09-27: modes from the operator
database, workflows from each default branch, and pull requests from `gh`.

| Project | Remote | Mode now | Shape | Train path | Required checks the planner derives | Before binding |
|---|---|---|---|---|---|---|
| outrider-ide | GitHub | disabled | `github_ci` | train | `Rust 1.89 minimum version`, `ubuntu-latest`, `windows-latest`, `macos-latest` | nothing (App mode: manifest + variables) |
| matter-engine-cpp | GitHub | development | `github_ci` | train | `Native Windows build and tests` | drain development; self-hosted `matter-engine-msvc` runner online |
| moss-and-spade-inventory-manager | GitHub | disabled | `github_ci_trigger_missing` | train | `Build & test` | CI must push-trigger on the train's refs |
| jackkern.com | GitHub | disabled | `github_ci_trigger_missing` | train | `Test, build, capture, budgets, links`, `Lighthouse budget` | CI must push-trigger on the train's refs |
| quilt-trader | GitHub | disabled | `github_no_ci` | train, once CI exists | `Tests` (from the template) | land a CI workflow; live-money rules below |
| rom-downloader | GitHub | disabled | `github_no_ci` | train, once CI exists | `Tests` (from the template) | land a CI workflow |
| matter-engine-web | on disk | disabled | `local_remote` | development train | local: `npm ci && npm test && npm run build` | none |
| quilt-trader-web | on disk | disabled | `local_remote` | development train | local: `npm ci && npm test && npm run build` | none |
| agent-queue-site | on disk | disabled | `local_remote` | development train | local: `pnpm install --frozen-lockfile && pnpm check` | none |

Every legacy pull request the cutover task named is already resolved (see
[Legacy pull requests](#legacy-pull-requests)).

## 1. The shared routes (once per install)

`parent-integration` and `root-train` are the reviewed agent-queue graphs,
unchanged, compiled at system scope. They have the same rules, steps, transitions
and command contracts as `agent-queue-parent-integration` and
`agent-queue-root-train`. Only the identity, scope and source references
differ. The daemon seeds both into `vault/reviewed-playbooks/` on start and
never activates them itself.

A system activation of either one fires only for a project whose frozen
policy route names it. Both ids are in `INTEGRATION_LIFECYCLE_PLAYBOOK_IDS`
(`src/playbooks/services.py`). Without that fence, one activation of
`parent-integration`, which subscribes to `task.completed`, would run
integration commands for every project on the install.

```bash
aq playbook v2-validate --path reviewed-playbooks/parent-integration/artifact.json
aq playbook v2-validate --path reviewed-playbooks/root-train/artifact.json
aq playbook v2-import --path reviewed-playbooks/parent-integration
aq playbook v2-import --path reviewed-playbooks/root-train
aq playbook activate --playbook-id parent-integration --artifact-sha256 sha256:65f970021fdf5c73b8aeabdd4bbbf22f2318d3c198d1176731b7196de1a3bf51 --enabled
aq playbook activate --playbook-id root-train --artifact-sha256 sha256:5808faf05a6723f49d510ced885cd4870fa3990a2f710c428ccf5a250874f2fe --enabled
aq playbook activation-health --playbook-id parent-integration
aq playbook activation-health --playbook-id root-train
```

Both validations must report zero errors or questions, and both imports must
report the two hashes above. Activation health must show each exact hash
enabled and ready. Activation publishes itself to the live runtime
(`_v2_publish_activation` refreshes the dispatch table), so no restart is
needed. A system-scoped activation is an install-wide change: a LOCAL operator
or the global supervisor runs it, not a project's supervisor.

## 2. The planner

```bash
aq integration onboard-train PROJECT \
  [--repo CHECKOUT] [--ref origin/main] \
  [--write-policy train-policy.PROJECT.json] \
  [--write-trust-manifest agent-queue-integration.json] \
  [--write-workflow ci.yml]
```

It reads the project record, its integration status and its completed and
BLOCKED tasks from the daemon. Then it reads `.github/workflows/*` and the
stack files (`package.json`, lockfiles, `pyproject.toml`,
`requirements.txt`, `.nvmrc`) at `origin/<default branch>` of the project's
workspace. It reads Git objects only: no fetch and no checkout. Fetch first if
the ref may be stale. It prints the commit it read. With `--repo` and
`--repo-url` it plans from Git alone when the daemon's records are out of
scope or unreachable. `--json` returns the whole plan.

How it decides:

- **Which workflows gate the train.** The CI observer accepts only check runs
  from a `push` run on the exact candidate
  (`AuthenticatedGitHubObserver(expected_event="push")`). The train pushes
  candidates to `aq/integration/**` and parent snapshots to `aq/parent/**`, so
  a workflow gates only when its push filter matches both. A path-filtered
  push never gates: a candidate may produce no check runs.
- **The check names.** Each gating job's `name`, or its id, expanded over its
  matrix. `${{ matrix.x }}` is substituted, and a name without a matrix
  expression gets GitHub's ` (v1, v2)` suffix. The matrix `include` and
  `exclude` rules are applied. Reusable-workflow calls, computed matrices,
  object-valued matrix suffixes and names reading other contexts are reported
  as problems, never guessed.
- **Jobs left out.** Every job of a deployment workflow (any job with an
  `environment`), and jobs whose `if:` provably skips push events. A job whose
  `if:` cannot be decided is kept and named in the problems.
- **The check-set version.** A digest of the names (`ci-<12 hex>`), so it
  changes exactly when they do. `--check-version` overrides it.
- **The route.** The project's own reviewed pair (`<project>-parent-integration`,
  `<project>-root-train`) when it ships, else the shared pair.
- **Classes and profiles.** `standard-high` for primary, verifier and debug
  work, on the derived `<class>-<harness>` rung (`--intelligence-class`,
  `--harness`; codex by default, matching agent-queue).

Before binding, confirm the names against a real push run on the default
branch or a branch that triggers CI:

```bash
gh api repos/OWNER/REPO/commits/SHA/check-runs --jq '.check_runs[] | [.name, .app.slug] | @tsv'
```

Run from agent-queue itself, the planner reproduces
[agent-queue-train-policy.json](agent-queue-train-policy.json) byte for byte
(existing-login mode, `--check-version tests-yml-v3`).

## 3. App credential mode

This install configures `integration.github_app` (the `agent-queue-train`
App, id 5075923), so the daemon runs in App credential mode. In that mode
`daemon_functional_preflight` (`src/integration/preflight.py`) takes the path
that needs repository-side trust, not the existing-login path:

- `.github/agent-queue-integration.json` on the default branch, schema
  `aq.integration-trust.v1`. Missing: `trust_manifest_unavailable`.
- Actions variables `AQ_INTEGRATION_ATTESTATION_APP_ID` (the App id) and
  `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` (the check-set version). Missing:
  `hosted_workflow_variables_unavailable`.
- A policy producer equal to the manifest's numeric `ci_producer_app_id`,
  which is 15368, GitHub Actions. The slug `github-actions` reads as
  `trust_manifest_mismatch`.

`--credential-mode auto` (the default) detects this from
`~/.agent-queue/config.yaml`, uses the producer `15368` and looks up the
repository's numeric id with `gh api`. `--write-trust-manifest` writes the
file, and the plan prints the two `gh variable set` commands. Commit the
manifest through the project's current delivery path before the observe
step. It can ride in the same change as a trigger fix or a CI workflow. The
reviewed agent-queue policy uses the slug, so the agent-queue cutover meets
the same three requirements while this mode is configured.

## 4. By shape

Every GitHub shape ends with the same script. The planner prints it with the
project's values, reading a fresh generation before each mutation:

1. drain, only when the project is not already `disabled`: `aq integration
   enable P --mode disabled ...`, then repeat status until `effective_mode`
   is `disabled` and `draining` is false;
2. bind the repository: `integration-repository-id` when one is designated,
   otherwise the `integration-repository` record from the project's URL and
   branch;
3. set review mode `pull_request`;
4. bind the policy from `--write-policy`;
5. enter observe, then `flush`, `adopt-legacy-deliveries --dry-run` and a
   `bind-legacy-repositories` preview;
6. run the ready check: status must report `ready` with zero blockers.

The planner never prints `--mode hierarchy` or `--mode train`. Those flips
belong to the supervisor after observe is clean (section 6).

### GitHub with CI: outrider-ide, matter-engine-cpp

```bash
aq integration onboard-train outrider-ide --write-policy train-policy.outrider-ide.json --write-trust-manifest agent-queue-integration.json
aq integration onboard-train matter-engine-cpp --write-policy train-policy.matter-engine-cpp.json --write-trust-manifest agent-queue-integration.json
```

- outrider-ide pushes CI on every branch and has no designated repository. The
  plan binds `{"id":"outrider-ide","url":"https://github.com/ElectricJack/outrider-ide.git","default_branch":"main"}`.
- matter-engine-cpp is in `development` (generation 9), so the plan starts
  with the drain. Its stored policy still names the retired system route
  `root-integration-train` from its 2026-09-09 train, and the policy binding
  replaces it. Its only check runs on the self-hosted `matter-engine-msvc`
  Windows runner, which must be online for any candidate to go green.

### CI that does not run on the train's refs: moss-and-spade-inventory-manager, jackkern.com

Both `ci.yml` files push-trigger on `main` only, so no candidate would ever
get a check run. The planner prints the trigger fix under **Trigger fix**:

```yaml
on:
  push:
    branches:
      - 'main'
      - 'aq/integration/**'
      - 'aq/parent/**'
```

Land that change through the project's current path (disabled mode: a pull
request a human merges), then run the planner again: it reclassifies as
`github_ci`. Notes:

- jackkern.com's `deploy.yml` publishes GitHub Pages on every push to `main`.
  It is a deployment workflow, never a required check. A train promotion
  fast-forwards `main` and deploys, exactly as a merged pull request does
  today.
- moss-and-spade-inventory-manager already designates `m&s-workspace-1`. The
  plan binds that id. Its second record, `m&s-workspace-2`, has no URL and is
  not used.

### GitHub without CI: quilt-trader, rom-downloader

The train gates only on GitHub check runs: CI receipts must come from an
authenticated `push` run on the exact candidate. A local command cannot
satisfy that gate. **These projects gate on a CI workflow**, which lands
first:

```bash
aq integration onboard-train quilt-trader --write-workflow ci.yml
```

The template has one job, `Tests`, which becomes the required check. It runs
on `pull_request` and on pushes to `aq/integration/**` and `aq/parent/**`,
with `contents: read`, no secrets and no deploy step. For a Python project it
runs `pip install -e '.[dev]'` (or `-r requirements.txt`) and then `pytest`.
Pass `--test-command` when the project needs something else. File the
workflow as a task in that project and merge it through its pull request.
Make `Tests` green on `main`, then run the planner again: it reclassifies as
`github_ci` with `Tests` as the check. A candidate cannot promote while the
suite is red, so fix red tests before the flip, not after.

The plan also prints an **optional interim**: the development train, using a
validation command that installs into a throwaway virtualenv outside AQ's
clone. Use it only if batched delivery must start before CI exists. It
publishes without a pull request, and the later train cutover starts by
draining it. For quilt-trader, keep pull request review until the workflow
lands.

### Local remotes: matter-engine-web, quilt-trader-web, agent-queue-site

These push to bare repositories under `~/.agent-queue/local-remotes/`.
Hierarchy and train need github.com (`delivery_path_problems` and the
`integration.delivery_path` doctor check), and no pull request can exist on
disk. Their train is the **development publisher**. It keeps the batch
journal, the pre-publish validation and the exact-lease push, needs no forge,
and replaces agile-ridge's per-task direct delivery for these projects:

```bash
aq integration onboard-train matter-engine-web
# prints:
aq integration develop matter-engine-web --validation focused --command 'npm ci && npm test && npm run build' --interval-seconds 300 --reason 'onto the development train: batched, validated delivery'
```

- `develop` designates the project's repository itself when the project has
  none (`development-<hash>` from the project URL). No binding step is needed.
- The validation command runs under `bash -c` in AQ's retained clone with the
  daemon's environment. The daemon's `PATH` must reach `node`/`npm`/`pnpm`
  (this box: `~/.local/share/pnpm`), or use absolute paths. `node_modules/`
  and `dist/` are git-ignored in all three repositories, so validation leaves
  the clone clean. Validation that leaves the tree dirty refuses publication.
- `aq task deliver` refuses a development project. The plan lists any BLOCKED
  task to deliver first. None was BLOCKED on 2026-09-27.
- agent-queue-site's `deploy.yml` would deploy a preview on every push,
  including a train candidate. That matters only if the site moves to GitHub.
  On disk no workflow runs.

## Legacy pull requests

The cutover task named quilt-trader #3-#9, rom-downloader #1-2, jackkern.com
#37-38 and outrider-ide #1-2 as open and approved. Read with `gh pr view` on
2026-09-27, all are resolved:

| Project | Merged | Closed |
|---|---|---|
| quilt-trader | #4, #7, #8, #9 | #3, #5, #6: their content is on `main` anyway (`scripts/health_watch.py` through #9, `/graft/` in `.gitignore`) |
| rom-downloader | #1, #2 | — |
| jackkern.com | #37, #38 | — |
| outrider-ide | #1, #2 | — |

The closed PRs' branches (`aq/sound-quest`, `aq/grand-ridge`,
`aq/grand-pinnacle`) are still on origin. They are harmless, and a human may
delete them. The train's cleanup deletes only the source branches of batches
it promoted.

Why this matters, and the procedure for any PR still open at cutover: each
such PR belongs to a COMPLETED standalone task with no repository binding.
Integration status ignores those tasks (`status.py`, "completed standalone
legacy tasks were never enrolled"), so they never block observe. By the same
token no train batch will ever deliver them. So, before the drain:

```bash
gh pr view URL --json state,reviewDecision,mergeable
aq git pr-merge --project-id PROJECT --pr-url URL --method merge
```

- Merge each approved, open PR while the project still uses its legacy review
  path. `--method merge` keeps the task's branch tip an ancestor of `main`, so
  `adopt-legacy-deliveries` can prove it if a hierarchy parent ever needs it.
  `aq git pr-merge` runs `gh` in the daemon's data directory, never in a
  project checkout (`src/commands/git_commands.py`).
- A PR that conflicts or is no longer wanted: close it, then refile the work
  as a new task after cutover, which the train delivers.
- A hierarchy child with a PR (matter-engine #11, `smart-dune.3`, merged) is
  covered by `adopt-legacy-deliveries` in observe.

`onboard-train` asks `gh` for each completed task's PR state (`--check-prs`,
the default) and puts only the still-open ones in this step.

## quilt-trader is live money

The coordinator (FastAPI on port 8000) runs from the working tree of
`~/dev/quilt-trader`, the project's primary workspace. Cron runs
`~/.quilt/bin/coord-watchdog.sh` every two minutes and at boot. It runs
`quilt down`/`quilt up` in that checkout only when `/api/health` fails. Three
live deployments on worker Trader1 trade real money. The cutover must neither
redeploy nor restart it:

- **What the cutover writes.** Database rows (policy, review mode, repository
  binding, rollout mode) and GitHub state. That is the CI workflow and trust
  manifest merged through pull requests, and later train refs pushed from AQ's
  own retained clone under `~/.agent-queue/`. No step checks out, pulls,
  resets or merges into `~/dev/quilt-trader`. Workers use worktree slots
  under `.aq/worktrees/`. Train and hierarchy delivery never run the
  per-task direct merge.
- **Never, during or after cutover:** `git pull`/`reset`/`checkout` in
  `~/dev/quilt-trader`, `quilt up`, `quilt down`, restarting the coordinator,
  or editing the watchdog. A CI workflow in this repository must not deploy,
  hold secrets, or reach Trader1. Deploying a new `main` to the coordinator
  stays Jack's manual decision.
- **Check after every mutation:**

  ```bash
  curl -fsS -m 5 http://localhost:8000/api/health
  tail -n 5 ~/.quilt/log/watchdog.log   # no new "restarting coordinator" line
  ```

- The `quilt-health-watch` playbook keeps running unchanged. Its check tasks
  are read-only and belong to the `quilt-health-checks` container, not to any
  integration train.

## 6. After observe: the supervisor's flips

Once observe reports ready with zero blockers, take a fresh generation and
flip each project, one at a time:

```bash
aq integration enable PROJECT --mode hierarchy --expected-generation "$(generation)" --reason 'enable recursive delivery'
aq integration enable PROJECT --mode train --interval-seconds 300 --expected-generation "$(generation)" --reason 'enable root train sweeps'
```

Suggested order, lowest risk first: outrider-ide, then matter-engine-cpp
(runner permitting), then moss-and-spade-inventory-manager and jackkern.com
once their trigger fixes merge, then rom-downloader and quilt-trader once their
CI is green. The local remotes can go at any time: `develop` is their whole
cutover. After each project's first promotion, confirm that `main` moved to
the tested candidate and that cleanup finished (`aq integration status`).

Rollback: `aq integration enable PROJECT --mode disabled --expected-generation
"$(generation)" --reason '...'` drains the train or the development publisher
and returns the project to its legacy review path. Nothing is deleted.
