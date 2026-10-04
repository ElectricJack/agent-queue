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

This install runs App credential mode, so each GitHub project also needs the
App-mode trust anchors: the manifest, the main push audit workflow, the Actions
variables and the ruleset. The planner prints them in order (section 3); the
end-to-end procedure, with the command output to expect, is
[app-mode-train.md](app-mode-train.md).

The mode flips are operator-scoped. A LOCAL operator, or a live named
supervisor of the project (or the global supervisor), runs the integration and
`aq project set` commands below. The once-per-install playbook import in
section 1 is a LOCAL operator action. A worker's token is refused at scope.

## Where each project stands

The table reflects the state read on 2026-09-27: modes from the operator
database, workflows from each default branch, and pull requests from `gh`.

| Project | Remote | Mode now | Shape | Train path | What gates delivery | Before binding |
|---|---|---|---|---|---|---|
| outrider-ide | GitHub | disabled | `github_ci` | train | `Rust 1.89 minimum version`, `ubuntu-latest`, `windows-latest`, `macos-latest` | nothing (App mode: manifest + variables) |
| matter-engine-cpp | GitHub | development | `github_ci` | train | `Native Windows build and tests` | drain development; self-hosted `matter-engine-msvc` runner online |
| moss-and-spade-inventory-manager | GitHub | disabled | `github_ci_trigger_missing` | train | `Build & test` | CI must push-trigger on the train's refs |
| jackkern.com | GitHub | disabled | `github_ci_trigger_missing` | train | `Test, build, capture, budgets, links`, `Lighthouse budget` | CI must push-trigger on the train's refs |
| quilt-trader | GitHub | disabled | `github_no_ci` | train, once CI exists | `Tests` (from the template) | land a CI workflow; live-money rules below |
| rom-downloader | GitHub | disabled | `github_no_ci` | train, once CI exists | `Tests` (from the template) | land a CI workflow |
| matter-engine-web | on disk | disabled | `local_remote` | development train | each task's close checks (`--validation none`: no job preset can install and test a Node project, [agile-pinnacle](#local-remotes-matter-engine-web-quilt-trader-web-agent-queue-site)) | none |
| quilt-trader-web | on disk | disabled | `local_remote` | development train | same as matter-engine-web | none |
| agent-queue-site | on disk | disabled | `local_remote` | development train | same as matter-engine-web | none |

Every legacy pull request the cutover task named is already resolved (see
[Legacy pull requests](#5-legacy-pull-requests)).

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
aq playbook activate --playbook-id parent-integration --artifact-sha256 sha256:67a4f1e68ce35dc2c2ec53611d246c84c709bf88d48e352ff1f15105c5ddfdf0 --enabled
aq playbook activate --playbook-id root-train --artifact-sha256 sha256:6681dd209a52e011b7f8f49f32a6d952782ec64a5e3df68da2ab7a34a01a25cf --enabled
aq playbook activation-health --playbook-id parent-integration
aq playbook activation-health --playbook-id root-train
```

Both validations must report zero errors or questions, and both imports must
report the two hashes above. Activation health must show each exact hash
enabled and ready. Activation publishes itself to the live runtime
(`_v2_publish_activation` refreshes the dispatch table), so no restart is
needed. A system-scoped activation is an install-wide change, and the shipped
supervisor profile grants none of the `playbook` import, validate or activate
commands, so a LOCAL operator runs this section.

## 2. The planner

```bash
aq integration onboard-train PROJECT \
  [--repo CHECKOUT] [--ref origin/main] \
  [--write-policy train-policy.PROJECT.json] \
  [--write-trust-manifest agent-queue-integration.json] \
  [--write-audit-workflow main-attestation.yml] [--write-ruleset ruleset.PROJECT.json] \
  [--write-workflow ci.yml]
```

It reads the project record, its integration status and its completed and
BLOCKED tasks from the daemon. Then it reads `.github/workflows/*` and the
stack files (`package.json`, lockfiles, `pyproject.toml`,
`requirements.txt`, `.nvmrc`) at `origin/<default branch>` of the project's
workspace. It reads Git objects only: no fetch and no checkout. Fetch first if
the ref may be stale. It prints the commit it read. Each daemon read is
optional. The shipped supervisor profile lacks `get_project`, so a supervisor
passes `--repo CHECKOUT`, and the planner takes the URL from that checkout's
`origin` and says so; the binding refuses a URL that differs from the project
record. With `--repo` and `--repo-url` it plans from Git alone when the daemon
is unreachable. `--json` returns the whole plan.

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
- **Jobs left out.** A deploy job (one with an `environment`) is never a
  check. When its `if:` is provably false on a train ref (a main-only deploy,
  `github.ref == 'refs/heads/main'`) the rest of the workflow stays CI.
  Otherwise the whole workflow is a deployment: its jobs are left out, the
  train's refs are never proposed for its trigger, and a note warns when they
  already run there. Jobs whose `if:` provably skips a push of a train ref are
  left out too, and so is every job that `needs` a skipped job, unless its own
  `if:` uses `always()`, `failure()` or `cancelled()`. GitHub skips such a job,
  and the observer counts a skipped required check as red. The `if:`
  evaluator knows `github.event_name`, `github.ref`, `github.ref_name` and the
  status functions. A job whose `if:` it cannot decide is kept and named in
  the problems.
- **Problems that stop the plan.** Two jobs producing the same check name (the
  observer reads only the newest run of a name, so one job could hide the
  other's failure), a repository URL that is not
  `https://github.com/OWNER/REPO.git` (the form the binding accepts), and an
  unknown repository id (status unreadable, or development mode, whose status
  omits it: pass `--repository-id`). The plan then ends at "Resolve the
  problems above".
- **The check-set version.** A digest of the names (`ci-<12 hex>`), so it
  changes exactly when they do. `--check-version` overrides it.
- **The route.** The project's own reviewed pair (`<project>-parent-integration`,
  `<project>-root-train`) when it ships, else the shared pair.
- **Classes.** `standard-high` for primary, verifier and debug work
  (`--intelligence-class`). It is a class hint only: those tasks are filed
  unrouted and the project's router assigns their profiles, so the policy
  names no profile.

Before binding, confirm the names against a real push run on the default
branch or a branch that triggers CI:

```bash
gh api repos/OWNER/REPO/commits/SHA/check-runs --jq '.check_runs[] | [.name, .app.id, .app.slug] | @tsv'
```

Run from agent-queue itself, the planner reproduces
[agent-queue-train-policy.json](agent-queue-train-policy.json) byte for byte
in either credential mode, with no `--check-version`: its check-set version is
the derived `ci-4c6e0c2a989c`.

## 3. App credential mode

The operator runbook for App mode, from prerequisites through cutover,
rotation, rollback and key rotation, is [app-mode-train.md](app-mode-train.md).
This section is the reference for what the daemon checks and what the planner
prints.

This install configures `integration.github_app` (the `agent-queue-train`
App, id 5075923), so the daemon runs in App credential mode. In that mode
`daemon_functional_preflight` (`src/integration/preflight.py`) takes the path
that needs repository-side trust, not the existing-login path. That path is
the App-mode item report (`src/integration/app_mode.py`), the same one
`aq integration app-verify` prints: every `fail` code is a blocker of the same
name, and every `warn` code is a non-blocking entry in the `warnings` list of
`aq integration status`.

- `credential`: an installation token mints for the repository. Otherwise
  `app_token_unavailable`; every item that reads GitHub then carries that code.
- `repository`: GitHub's id, name and default branch equal the binding and
  the AQ record. Otherwise `repository_mismatch`.
- `producer`: a numeric policy producer on both boundaries, a positive
  decimal with no sign or leading zero. A legacy slug such as `github-actions`
  is `ci_producer_not_numeric`; two different numbers are `ci_producer_mismatch`.
- `manifest`: `.github/agent-queue-integration.json` on the default branch,
  schema `aq.integration-trust.v1`, equal on every identity field. Missing or
  unparseable: `trust_manifest_unavailable`. A different identity, such as a
  `ci_producer_app_id` other than the policy producer (15368, GitHub Actions),
  is `trust_manifest_mismatch`. A check set that differs from the bound policy
  only warns, `trust_manifest_check_set_differs`: it is expected during a
  rotation, and the frozen snapshot owns the runtime check set.
- `variables`: Actions variables `AQ_INTEGRATION_ATTESTATION_APP_ID` (the
  daemon's App id) and `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` (the bound
  policy's root check-set version). They are compared with those two values
  only, never with the manifest. Missing: `hosted_workflow_variables_unavailable`;
  different: `hosted_workflow_variables_mismatch`.
- `protection`: the default branch's rules, read through the App
  (`src/integration/protection.py`): the effective rules, including parent and
  organization rulesets, the App's own `current_user_can_bypass` for each
  ruleset, and classic protection. They are classified as `attested_only` (the
  App cannot bypass, and the attestation pinned to its id is required),
  `app_bypass`, `incompatible` (a rule the App cannot bypass would refuse an
  attested fast-forward, such as a pull-request or signature rule, or a
  required check that is neither the pinned attestation nor a policy check
  pinned to the CI producer), `unprotected`, or `unverifiable` (a read failed).
  The observe, hierarchy and train modes need `attested_only`, and a disabled
  project is judged the same way because it is being readied for them:
  `app_bypass` warns (`main_protection_app_bypass`), and the other three block
  (`branch_protection_incompatible`, `main_protection_missing`,
  `main_protection_unverifiable`). An unreadable protection is never
  "unprotected". A development project needs the App's unattested push to get
  through: `aq integration develop` is refused with
  `main_protection_blocks_development_publisher` while a rule the App cannot
  bypass would refuse it, which includes `attested_only` and `incompatible`.
  The refusal names each such ruleset or classic protection setting. An
  unverifiable reading does not refuse: it is returned as the command's
  `evidence.protection`. `app-setup` prints the §8.1 target ruleset only for
  the codes it fixes, and a `PUT` only to a ruleset already named
  `Train-only <branch>`. Nothing in AQ writes a rule.
- `audit_workflow`: a default-branch workflow that reads
  `vars.AQ_INTEGRATION_ATTESTATION_APP_ID`. Missing only warns,
  `audit_workflow_missing`.

The planner emits the producer `15368` in both credential modes, so a policy
it writes binds under either one without a rebind. Existing-login credentials
still accept a slug that an older policy holds. `--credential-mode auto` (the
default) detects App mode from `~/.agent-queue/config.yaml` and looks up the
repository's numeric id with `gh api`. In App mode the plan orders the trust
anchors around the drain (App-mode spec §11; [app-mode-train.md
§9.7](app-mode-train.md#97-every-other-project)):

1. **App mode: land the trust manifest and the audit workflow** through the
   project's current delivery path. Both are inert until the variables are
   set, and they can ride in the same change as a trigger fix or a CI
   workflow. With a designated repository the manifest comes from
   `aq integration trust-manifest PROJECT --policy train-policy.PROJECT.json
   --repository-id ID --write .github/agent-queue-integration.json`; before
   the bind creates the repository record, `--write-trust-manifest PATH`
   writes the same bytes locally. `--write-audit-workflow PATH` renders
   `main-attestation.yml`.
2. The drain.
3. **App mode: set the Actions variables (repository admin)**: `aq integration
   app-setup PROJECT --policy ... --repository-id ID --apply`, or the two `gh
   variable set` commands before a repository record exists.
4. **App mode: require the attestation on main (repository admin)**: the
   ruleset `--write-ruleset PATH` writes (the §8.1 shape: the attestation
   pinned to the App, no bypass), posted with `gh api`, then `app-verify`.
5. The bind, then observe, where `aq integration app-verify PROJECT` checks
   every item against the bound policy.

The rendered audit workflow embeds `src/integration/hosted_attestation.py`
verbatim and runs on `ubuntu-latest` whatever runners the project's CI uses.
For an unattested push it re-runs each gating CI workflow through
`workflow_call` when that is safe: the workflow declares `workflow_call`, has
no job with an `environment`, needs no inputs or secrets, and asks for no
permission beyond `contents: read` and `checks: read`. A workflow with an
`environment` is never called, because a called workflow runs with the
caller's `main` ref and a main-only deploy would fire. Any gating workflow it
cannot call is replaced by a failing `Unattested push to main` job. The CI
template `--write-workflow` writes declares `workflow_call`. The App's single
installation must cover every target repository, with Actions variables
readable; otherwise status reports `repository_binding_failed` or
`hosted_workflow_variables_unavailable`. The reviewed agent-queue policy
already names `15368`, and its manifest and audit workflow are committed, so
the agent-queue cutover needs the variables and the ruleset
([app-mode-train.md §9.3](app-mode-train.md#93-cutover-supervisor-with-the-repository-admin-at-the-marked-steps)).

The manifest is reviewed repository content, so its initial commit may pass
through the development publisher. Later check-set rotations may update it
through the train while the frozen policy snapshot still requires the old
checks. AQ's reserved delivery-path guard protects daemon bookkeeping files,
not this manifest. App-mode preflight and subject-trust checks still compare
its identity fields to the binding, App and policy; the tree's check list is
informational during a rotation. Actions variables and rulesets remain
operator-managed trust anchors.

The daemon renders the same manifest from what it actually trusts:

```text
aq integration trust-manifest PROJECT [--policy FILE] [--repository-id ID] \
    (--write PATH | --check | --print) [--json]
```

It builds from `--policy FILE`, or the bound policy when that is omitted. The
repository is `--repository-id`, or the designated one. `repository_id` and
`full_name` come from the authenticated GitHub binding, `attestation_app_id`
from the daemon's App, `ci_producer_app_id` from the policy producer and the
check set from the policy's `root` boundary, in policy order. The text is
`json.dumps(manifest, indent=2, sort_keys=True)` plus a newline, the bytes
`--write-trust-manifest` writes too (`src/integration/trust_manifest.py` is the
one builder). The result also compares the copy committed on the default branch,
read through the App at that branch's exact SHA. `--check` exits 1 when that
copy is missing, unparseable or differs on an identity field (the repository
ids, the name, either App id, the schema), and prints the field diff and the
`--write` command. A check-set or formatting difference is only a warning
(`trust_manifest_check_set_differs`, `trust_manifest_noncanonical`), because the
frozen policy snapshot owns the runtime check set. The command is read-only; it
is refused with `not_app_mode` under existing-login credentials, with
`ci_producer_not_numeric` for a slug producer, and at scope for worker tokens.
The shipped supervisor profile holds the grant. For agent-queue:

```bash
aq integration trust-manifest agent-queue --policy docs/config/agent-queue-train-policy.json \
    --repository-id agent-queue2 --write .github/agent-queue-integration.json
```

That file is committed. `tests/test_integration_trust_manifest.py` pins it to
the builder's output for the reviewed policy, so a change to the policy's check
set must regenerate it with this command and commit both files together.

Two more commands check and set up the rest:

```text
aq integration app-verify PROJECT [--policy FILE] [--repository-id ID] [--json]
aq integration app-setup PROJECT [--policy FILE] [--repository-id ID] [--apply]
```

`app-verify` (daemon command `integration_app_verify`, read-only) prints the
seven items, each `ok`, `warn` or `fail` with its code, what was expected, what
GitHub showed and the exact fix, and exits 1 when an item fails. `--json` adds
`expected`: the manifest text, the two variable values and the target ruleset
for the default branch, which requires the attestation pinned to the App's
integration id and has no bypass. Existing-login credentials are reported as
the `credential` item's `not_app_mode`, not refused. The shipped supervisor
profile holds the grant.

`app-setup` runs locally. It calls `app-verify` and prints, per concern, what is
wrong and the fix. For the variables it prints `gh variable set NAME --repo
OWNER/REPO --body VALUE` for each variable that differs. With `--apply` it runs
those commands with your own `gh` login, which must be a repository admin, then
verifies again and prints the `variables` item. A variable that is already
correct is never touched. For the manifest it prints the `trust-manifest
--write` command and where to commit the file. For protection it prints the
target ruleset JSON and the `gh api --method PUT|POST …/rulesets` command. It
never writes repository files and never applies a ruleset: the daemon's App
holds no write permission over its own trust anchors, so these writes are the
operator's.

## 4. By shape

A plan stops at the first prerequisite that changes the repository: a CI
workflow, a trigger fix, or the App-mode manifest riding with one. After that
change reaches the default branch, run the planner again. Once CI gates, the
plan ends with the same script for every GitHub project, printed with the
project's values and a fresh generation read before each mutation:

1. drain, only when the project is not already `disabled` (with status
   unreadable, the plan prints the drain as conditional): `aq integration
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

In App credential mode the manifest and audit workflow land before the drain,
and the Actions variables and the ruleset come between the drain and the bind
(section 3).

The planner never prints `--mode hierarchy` or `--mode train`. Those flips
belong to the supervisor after observe is clean (section 7).

### GitHub with CI: outrider-ide, matter-engine-cpp

```bash
aq integration onboard-train outrider-ide --write-policy train-policy.outrider-ide.json --write-trust-manifest agent-queue-integration.json --write-audit-workflow main-attestation.yml --write-ruleset ruleset.outrider-ide.json
aq integration onboard-train matter-engine-cpp --write-policy train-policy.matter-engine-cpp.json --write-trust-manifest agent-queue-integration.json --write-audit-workflow main-attestation.yml --write-ruleset ruleset.matter-engine-cpp.json
```

- outrider-ide pushes CI on every branch and has no designated repository. The
  plan binds `{"id":"outrider-ide","url":"https://github.com/ElectricJack/outrider-ide.git","default_branch":"main"}`.
- matter-engine-cpp is in `development` (generation 9), so the plan starts
  with the drain. Development-mode status does not report the designated
  repository, so pass `--repository-id matter-engine-cpp` (its existing
  record). Its stored policy still names the retired system route
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
`github_ci`. Until then the plan stops after this step, because the observe
preflight never looks at CI: a train bound before the fix would pass its ready
check and then never see a check run. Notes:

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
on `pull_request` and on pushes to `main`, `aq/integration/**` and
`aq/parent/**`, with `contents: read`, no secrets and no deploy step. For a
Python project it runs `pip install -e '.[dev]'` (or `-r requirements.txt`) and
then `pytest`. quilt-trader's tests likely also need its `coordinator` and
`worker` extras. The task that lands the workflow adjusts the install line,
and `--test-command` replaces the steps outright. File the workflow as a task
in that project and merge it through its pull request. Make `Tests` green on
`main`, then run the planner again: it reclassifies as `github_ci` with
`Tests` as the check. A candidate cannot promote while the suite is red, so
fix red tests before the flip, not after.

There is no local-validation interim. The development publisher validates
only through the job queue's fixed presets (next section), and none of them
can install these projects' dependencies. Keep each project's pull request
review until its workflow lands.

### Local remotes: matter-engine-web, quilt-trader-web, agent-queue-site

These push to bare repositories under `~/.agent-queue/local-remotes/`.
Hierarchy and train need github.com (`delivery_path_problems` and the
`integration.delivery_path` doctor check), and no pull request can exist on
disk. Their train is the **development publisher**. It keeps the batch
journal, dependency ordering and the exact-lease push, needs no forge, and
replaces agile-ridge's per-task direct delivery for these projects:

```bash
aq integration onboard-train matter-engine-web --repo ~/dev/websites/matter-engine
# prints:
aq integration develop matter-engine-web --validation none --interval-seconds 300 --reason 'onto the development train: batched, lease-guarded delivery'
```

- `develop` designates the project's repository itself when the project has
  none (`development-<hash>` from the project URL). No binding step is needed.
- **Validation is `none`, and that is deliberate.** Development validation runs
  no shell. `src/integration/development_validation.py` submits each command
  to the job queue, which maps it onto a fixed preset
  (`src/jobs/adapters.finite_command`). The presets are `pytest` or `aq test`
  in the daemon's own interpreter, `ruff check`, and `npm run build` with no
  arguments through `/usr/bin/npm`, which this box does not have. Anything
  else is refused as an infrastructure outcome, and that defers every batch
  forever. `develop` does not reject it up front. No preset can install and
  test a Node project, so each task's own close checks remain the gate, as
  they were under direct delivery. The planner leaves out any
  `--validation-command` that is not a preset, and names it in the problems.
  Task `agile-pinnacle` tracks adding install and test presets, rejecting
  non-preset commands at configure time, and correcting the development
  docs, which still describe `bash -c`.
- `aq task deliver` refuses a development project. The plan lists any BLOCKED
  task to deliver first. None was BLOCKED on 2026-09-27.
- agent-queue-site's `deploy.yml` would deploy a preview on every push,
  including a train candidate. That matters only if the site moves to GitHub.
  On disk no workflow runs.

## 5. Legacy pull requests

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

## 6. quilt-trader is live money

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
- **The main push audit** (`--write-audit-workflow`) holds no secret and
  deploys nothing, and the planner never names a fallback workflow that
  declares an `environment`: such a workflow is replaced by a failing
  `Unattested push to main` job instead of being called on `main`.
- **Check after every mutation:**

  ```bash
  curl -fsS -m 5 http://localhost:8000/api/health
  tail -n 5 ~/.quilt/log/watchdog.log   # no new "restarting coordinator" line
  ```

- The `quilt-health-watch` playbook keeps running unchanged. Its check tasks
  are read-only and belong to the `quilt-health-checks` container, not to any
  integration train.

## 7. After observe: the supervisor's flips

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
