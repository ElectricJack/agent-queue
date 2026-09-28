# App-mode integration train: operator runbook

App credential mode is how a production AQ install runs the integration train.
It is on when `~/.agent-queue/config.yaml` sets `integration.github_app`; on this
install that is the App `agent-queue-train` (id 5075923, installation
164874645). Existing-login credentials (`gh` as the daemon user) remain for
development installs without an App
([hierarchical-integration-trains.md §2](../guides/hierarchical-integration-trains.md#2-github-credentials-app-mode-or-existing-login)).

In App mode the train trusts three things:

1. **CI**, produced by GitHub Actions (App 15368) on the exact candidate SHA.
2. **The attestation**: a check run named `Agent Queue Integration Attestation`
   that the daemon publishes through its own App after it has verified CI. Only
   a holder of that App's private key can create a check run attributed to it.
3. **Repository-side trust anchors**, which nothing in AQ writes:

   | Anchor | What it is | Written by |
   |---|---|---|
   | Trust manifest | `.github/agent-queue-integration.json`: the repository, the CI producer, the attestation App | reviewed repository content, rendered by `aq integration trust-manifest` |
   | Actions variables | `AQ_INTEGRATION_ATTESTATION_APP_ID`, `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` | the repository admin's `gh` (`aq integration app-setup --apply`) |
   | Ruleset | `Train-only main`: the attestation required, pinned to the App, no bypass | the repository admin (`gh api`) |
   | Audit workflow | `.github/workflows/main-attestation.yml`: full CI after any unattested push to `main` | reviewed repository content |

The daemon's App can read every anchor and write none of them (spec invariant
I5). The design is the vault spec *App-mode integration train: production GitHub
App credentials* (`projects/agent-queue/specs/2026-09-27-app-mode-integration-train-production-github-app-credentials.md`,
review `rev-swift-glacier`). Section numbers here follow its §9, and the daemon's
fix messages cite them as "runbook §9.x". Sections 9.1-9.6 are written for
agent-queue; §9.7 covers every other project.

## Commands

```text
aq integration trust-manifest PROJECT [--policy FILE] [--repository-id ID] (--write PATH | --check | --print) [--json]
aq integration app-verify PROJECT [--policy FILE] [--repository-id ID] [--json]
aq integration app-setup PROJECT [--policy FILE] [--repository-id ID] [--apply]
aq integration onboard-train PROJECT [--write-policy PATH] [--write-trust-manifest PATH] [--write-audit-workflow PATH] [--write-ruleset PATH]
aq doctor --check integration.app_mode
```

- `trust-manifest` and `app-verify` are read-only daemon commands. The shipped
  supervisor profile holds both grants; a worker token is refused at scope.
  `trust-manifest` is refused with `not_app_mode` under existing-login
  credentials.
- `app-setup` runs in your shell. It calls `app-verify`, prints the fix for
  each concern, and with `--apply` runs `gh variable set` with your own login
  for each variable that differs. It never writes repository files and never
  applies a ruleset.
- Each takes the policy from `--policy FILE`, so the anchors can be prepared
  before the policy is bound, else the project's bound policy. `--repository-id`
  defaults to the project's designated integration repository.
- `app-verify` reports one item per concern, each `ok`, `warn` or `fail` with
  a code, what was expected, what GitHub showed and the exact fix: `credential`,
  `repository`, `producer`, `manifest`, `variables`, `protection`,
  `audit_workflow`. It exits 1 when an item fails. The functional preflight
  reads the same items, so every `fail` code is an `aq integration status`
  blocker of the same name and every `warn` code a status warning. The codes
  are listed in [train-onboarding.md §3](train-onboarding.md#3-app-credential-mode).

The `generation()` helper and the bind commands are the ones in
[agent-queue-train-policy.md](agent-queue-train-policy.md):

```bash
generation() {
  aq --json integration status agent-queue |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["generation"])'
}
```

## 9.1 Prerequisites (once per install)

- `integration.github_app` is configured (`app_id`, `installation_id`,
  `private_key_path`). The private key is readable only by the daemon user:
  `OwnerFilePrivateKeyProvider` refuses anything broader than 0600.
- The App registration grants exactly `actions: read`, `actions_variables:
  read`, `administration: read`, `checks: write`, `contents: write`, `issues:
  write`, `metadata: read`, `pull_requests: write` and `workflows: write`. The
  token mint refuses a grant that differs, and `app-verify` reports it as
  `credential` `app_token_unavailable`.
- The installation covers every repository a train will use.
- The shared or project routes are imported and active
  ([train-onboarding.md §1](train-onboarding.md#1-the-shared-routes-once-per-install),
  [agent-queue-train-policy.md](agent-queue-train-policy.md)).

## 9.2 Land the repository artifacts (development mode, before the drain)

Every artifact is inert until the project runs App mode, so each lands through
the project's current delivery path. For agent-queue that is the development
publisher, which pushes with the App's bypass on ruleset 24002443.

1. The implementation (epic `noble-impact-73`).
2. The reviewed policy with the numeric producer `"15368"`
   ([agent-queue-train-policy.json](agent-queue-train-policy.json)).
3. `.github/agent-queue-integration.json`, produced by:

   ```bash
   aq integration trust-manifest agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2 --write .github/agent-queue-integration.json
   ```

   ```text
   wrote .github/agent-queue-integration.json (sha256 b175798d0ac43fa92e03fd0ee93b652242d4d8eb318c3c4bc3f232a2a23cb6c3) for ElectricJack/agent-queue (1160639300), App 5075923, argument policy
   committed copy matches: .github/agent-queue-integration.json on main (<default-branch sha>)
   ```

   For agent-queue this step is done: the operator committed the file as
   `64fa07ad6`, byte-identical to the builder's output.
   `tests/test_integration_trust_manifest.py` pins it to the reviewed policy,
   so a check-set change that does not regenerate it fails CI. `--check` is the
   read-only confirmation. It exits 1 on a missing file or an identity
   difference, prints the field diff and the `--write` command, and only warns
   on a check-set difference (`trust_manifest_check_set_differs`):

   ```text
   $ aq integration trust-manifest agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2 --check
   committed copy matches: .github/agent-queue-integration.json on main (<default-branch sha>)
   ```

4. `.github/workflows/main-attestation.yml`, `src/integration/hosted_attestation.py`
   and `workflow_call:` in `tests.yml`. Also done for agent-queue. With the
   variables unset the audit reports `configured=false` and runs nothing.

## 9.3 Cutover (supervisor, with the repository admin at the marked steps)

Preconditions: every node of the implementation epic is complete and on
`main`, and the disposable-repository gate report shows S1-S10 passing, or each
gap accepted by the operator.

1. **Verify before touching anything:**

   ```text
   $ aq integration app-verify agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2
   App mode for agent-queue: agent-queue2 (ElectricJack/agent-queue 1160639300), App 5075923, argument policy
     ok   credential
     ok   repository
     ok   producer
     ok   manifest
     fail variables       hosted_workflow_variables_unavailable
          expected: {"AQ_INTEGRATION_ATTESTATION_APP_ID": "5075923", "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": "tests-yml-v3"}
          observed: {"unavailable": {"AQ_INTEGRATION_ATTESTATION_APP_ID": "absent", "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": "absent"}, "values": {"AQ_INTEGRATION_ATTESTATION_APP_ID": null, "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": null}}
          fix: aq integration app-setup agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2 --apply (runbook §9.3 step 3)
     ok   protection      app_bypass
     ok   audit_workflow
   not ready: 1 blocker(s), 0 warning(s)
   ```

   Expected: `manifest` ok, `variables` fail (unset), and `protection`
   classified `app_bypass`, which a development project requires. Any other
   failure is fixed first, with the command its `fix:` line names.

2. **Drain development:**

   ```bash
   aq integration enable agent-queue --mode disabled --expected-generation "$(generation)" --reason 'drain development publisher for the App-mode cutover'
   aq integration status agent-queue
   ```

   Repeat status until `effective_mode` is `disabled` and `draining` is false.

3. **[Repository admin] Set the variables:**

   ```text
   $ aq integration app-setup agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2 --apply
   App-mode setup for agent-queue: ElectricJack/agent-queue (1160639300), App 5075923

   variables
     fail variables       hosted_workflow_variables_unavailable
     AQ_INTEGRATION_ATTESTATION_APP_ID: set
     AQ_INTEGRATION_REQUIRED_CHECK_VERSION: set
     re-verified:
     ok   variables

   manifest
     ok   manifest

   protection
     warn protection      main_protection_app_bypass (app_bypass)
     the target ruleset (save it as FILE; applying it is the repository admin's step, runbook §9.3 step 4):
   {
     "name": "Train-only main",
     ...
   }
     gh api --method PUT repos/ElectricJack/agent-queue/rulesets/24002443 --input FILE
   ```

   The re-verify must show `variables` ok. Without `--apply` the command prints
   the two `gh variable set NAME --repo ElectricJack/agent-queue --body VALUE`
   commands instead. A disabled project is judged like the train modes, so the
   bypass is now a warning (`main_protection_app_bypass`).

4. **[Repository admin] Apply the ruleset.** Save the target ruleset that
   step 3 printed (it is also `expected.ruleset` in `app-verify --json`) and
   replace ruleset 24002443 with it:

   ```bash
   aq --json integration app-verify agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2 | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["data"]["expected"]["ruleset"], indent=2))' > ruleset.agent-queue.json
   gh api --method PUT repos/ElectricJack/agent-queue/rulesets/24002443 --input ruleset.agent-queue.json
   ```

   The ruleset requires `Agent Queue Integration Attestation` with
   `integration_id` 5075923 and has no bypass actor. "Basic Protection"
   (21960669) stays as it is. `app-verify` must now report `protection`
   `attested_only`:

   ```text
   $ aq integration app-verify agent-queue --policy docs/config/agent-queue-train-policy.json --repository-id agent-queue2
   App mode for agent-queue: agent-queue2 (ElectricJack/agent-queue 1160639300), App 5075923, argument policy
     ok   credential
     ok   repository
     ok   producer
     ok   manifest
     ok   variables
     ok   protection      attested_only
     ok   audit_workflow
   ready: 0 blocker(s), 0 warning(s)
   ```

   Nothing pushes `main` from here until the first promotion.

5. **Bind the repository, review mode and policy**, enter `observe`, `flush`,
   adopt legacy deliveries, and wait for `ready` with zero blockers. These are
   the handoff steps in [agent-queue-train-policy.md](agent-queue-train-policy.md),
   with the numeric-producer policy. After the bind, `app-verify` without
   `--policy` reads the bound policy:

   ```bash
   aq integration app-verify agent-queue
   aq integration status agent-queue
   ```

   Status lists `app-verify`'s `warn` codes under `warnings`, which never block
   readiness.

6. **`hierarchy`, then `train`**: the supervisor's flips.
7. **First promotion checks:**
   - the candidate SHA carries `Agent Queue Integration Attestation` from App
     5075923;
   - `main` equals the candidate;
   - the `Main attestation` run on `main` shows `attested=true`, and its
     step summary reads `Main attestation: attested (attested by check run
     <id> from App 5075923; 15 checks re-verified).`;
   - `unattested-ci` was skipped;
   - `aq integration status agent-queue` shows cleanup complete.
8. **Retire `train-candidate.yml`** in a task delivered through the train. No
   rule requires its `aq-train/candidate` status after step 4, and it holds
   `statuses: write`.

If GitHub rejects the promotion push, it fails closed and `main` is unchanged.
Fix the cause `app-verify` names and let the train retry. Never re-add the
bypass to get past a rejection.

## 9.4 How the manifest and workflow changes themselves land

- **Initial landing** is §9.2: the development publisher, with the App bypass
  that ruleset 24002443 already grants, before the drain. The strict ruleset is
  applied only after the drain (§9.3 step 4), so no step needs a bypass that is
  not already configured. The reserved delivery-path guard protects `.aq/**`,
  `.aq-worktree.json` and `.codex/**`, not the manifest.
- **In steady state** every change goes through the train, workflow changes
  included: the App's token has `workflows: write`, so a candidate that edits
  `.github/workflows/*` can be pushed and promoted. A workflow change that keeps
  the check set needs nothing else; a check-set change follows §9.5.
- **Identity changes** are re-cutovers in a maintenance window: a repository
  rename or transfer (`full_name`), or a new App (`attestation_app_id` and the
  ruleset's `integration_id`). Drain to `disabled`; the repository admin
  restores the App bypass; re-enter development (`aq integration develop
  agent-queue --reason '...'`, with the validation flags it had before the
  cutover); land the regenerated manifest; then §9.3 from step 2. Every subject tree carries the
  old identity until the new manifest lands, so no train can land it.

## 9.5 Changing the required check set (rotation)

The frozen policy snapshot alone decides which checks are required; a subject
tree's manifest is compared on identity only. So a rotation needs no bypass and
no development detour. **Expand, switch, contract:**

1. **Expand.** A task changes the workflows so that every current required
   name still runs and the new names run too. It regenerates the manifest with
   `aq integration trust-manifest agent-queue --policy NEW.json --write .github/agent-queue-integration.json`
   and commits `NEW.json` as the project's reviewed policy file. It lands
   through the train under the old policy, where the old names are green.
2. **Switch.** Drain to `disabled`, bind `NEW.json`, then
   `aq integration app-setup agent-queue --policy NEW.json --apply`, which
   updates `AQ_INTEGRATION_REQUIRED_CHECK_VERSION`. Then `observe` (no
   `trust_manifest_check_set_differs` any more), `hierarchy`, `train`.
3. **Contract.** A task removes the jobs only the old set needed. It lands
   under the new policy.

A **hard switch** is allowed when duplicating the old jobs is impractical: one
task changes the workflows and the manifest together. Bind `NEW.json` only after
that task is an approved, eligible root, so the next sweep includes it; a batch
without it goes red on the missing new checks.

**Parents.** A parent episode that starts after the switch, on a branch whose
tree predates the expand step, produces only the old names. It goes red on
missing checks, and its repair must merge the default branch into the parent
branch. Switch when `aq integration status` shows no parent collection on such
a branch.

The ruleset needs no change at any step: it requires only the attestation.

## 9.6 Rollback, key rotation and verification

- **Rollback to development:** the repository admin first restores the App
  bypass, adding
  `{"actor_id": 5075923, "actor_type": "Integration", "bypass_mode": "always"}`
  to the ruleset's bypass actors. Then re-enter development with
  `aq integration develop agent-queue --reason '...'` and the validation flags
  it had before the cutover. While the protection is `attested_only` or
  `incompatible`, `develop` is refused with
  `main_protection_blocks_development_publisher`; better a refusal than a
  publisher whose every push GitHub rejects.
  **Rollback to disabled** needs no ruleset change.
- **Private key rotation:**
  1. Generate a new key in the App settings.
  2. Replace the file at `private_key_path` (same path, owner-only).
  3. `aq restart --no-dashboard`.
  4. `aq integration app-verify agent-queue`: the `credential` item must be ok.
  5. Delete the old key on GitHub.
- **Routine check:** `aq integration app-verify PROJECT` is read-only and safe
  at any time. `aq doctor --check integration.app_mode` runs it for every
  enabled GitHub project while `integration.github_app` is configured. Each
  `fail` item is an ERROR finding and each `warn` item a WARN finding; the
  check's `data.findings` lists the project, item, codes and fix. A command
  refusal for an enabled project (no binding, no designated repository) is an
  ERROR too. Disabled projects and local remotes are skipped. A development
  project is judged on the items its publisher depends on (`credential`,
  `repository`, `protection`); the manifest, variables and audit are the train's,
  unset by design until the cutover. A development project with no bound
  policy is listed as not verified.

**Break-glass.** The repository admin adds a bypass actor (themselves or the
admin role), pushes, and removes it again. `main-attestation.yml` finds no
attestation and runs full CI on the new `main`; its summary reads
`Main attestation: NOT attested (...); unattested-ci runs the full suite.` The
ruleset history on GitHub is the audit trail. AQ does not model break-glass.

## 9.7 Every other project

App mode is install-wide: with `integration.github_app` set, every GitHub
project on the train runs it. Each needs the same four anchors. Local-remote
projects use the development publisher and are unaffected. The planner prints
the sequence with the project's values:

```bash
aq integration onboard-train PROJECT --write-policy train-policy.PROJECT.json --write-audit-workflow main-attestation.yml --write-ruleset ruleset.PROJECT.json
```

In App mode (`--credential-mode auto` reads `integration.github_app`) the plan
orders the anchors around the drain:

1. **App mode: land the trust manifest and the audit workflow**, through the
   project's current path (disabled mode: a pull request a human merges). With
   a designated repository the manifest comes from
   `aq integration trust-manifest PROJECT --policy train-policy.PROJECT.json --repository-id ID --write .github/agent-queue-integration.json`;
   before the bind creates the repository record the daemon cannot resolve
   it, and `--write-trust-manifest PATH` writes the same bytes.
2. **Drain** (when the project is not already disabled).
3. **App mode: set the Actions variables (repository admin):** `aq integration
   app-setup PROJECT --policy ... --repository-id ID --apply`, or the two `gh
   variable set` commands before a repository record exists.
4. **App mode: require the attestation on main (repository admin):** the
   `--write-ruleset` file posted with `gh api --method POST
   repos/OWNER/REPO/rulesets --input ruleset.PROJECT.json` (or `PUT` to the
   ruleset already named `Train-only main`), then `app-verify`.
5. **Bind** the repository, review mode and policy.
6. **Observe**, with `aq integration app-verify PROJECT` against the bound
   policy, then the ready check.

`--write-audit-workflow PATH` renders `main-attestation.yml` for the project:

- The verify step embeds `src/integration/hosted_attestation.py` verbatim, in a
  quoted heredoc the runner writes to `$RUNNER_TEMP` and runs with `python3`.
  `tests/test_train_onboarding.py` asserts the embedded copy equals the module
  and runs the rendered step. Regenerate the file after a verifier change; never
  edit it by hand.
- The audit job runs on `ubuntu-latest` even when the project's CI is
  self-hosted (matter-engine-cpp).
- **The fallback** calls each gating CI workflow through `workflow_call`
  (`unattested-ci`) when it can do so safely: the workflow declares
  `workflow_call`, has no job with an `environment`, requires no inputs or
  secrets, and asks for no permission beyond `contents: read` and `checks:
  read`. A called workflow runs with the caller's ref, so a deploy job gated on
  `main` would run on every unattested push: the planner never names a workflow
  that declares an `environment` (quilt-trader's rule). Any gating workflow it
  cannot call is replaced by an `Unattested push to main` job that fails with a
  summary naming it, so the push is still visible. The rendered workflow holds
  no secret and deploys nothing.
- The planner's own CI template (`--write-workflow`) declares `workflow_call`,
  so a project that adopts it gets the full fallback.
- The audit workflow never gates the train itself: the planner recognizes it
  by `vars.AQ_INTEGRATION_ATTESTATION_APP_ID` and excludes its jobs from the
  check set.

agent-queue keeps its own `main-attestation.yml`, which reads the verifier
from a sparse checkout of the pushed tree instead of embedding it; the rendered
file for agent-queue calls `tests.yml` exactly as that one does.

## Failure modes

| Situation | What happens | Surface |
|---|---|---|
| Manifest missing on the default branch | Observe not ready | blocker `trust_manifest_unavailable`; `app-verify` prints the `trust-manifest --write` command |
| Subject tree lacks the manifest or names another identity | The subject is refused, never promoted | status `subject_trust_invalid` with the subject and cause |
| Subject tree check set differs from the snapshot | Allowed: the snapshot decides, and missing checks turn red | observer evidence; repair |
| Variables missing or stale | Observe not ready; the audit is unconfigured or mismatched, so full CI runs on `main` | blocker `hosted_workflow_variables_*` |
| Slug producer in App mode | Observe not ready | blocker `ci_producer_not_numeric` |
| Ruleset missing or unpinned | Observe not ready | blocker `main_protection_missing` |
| Ruleset refuses an attested fast-forward | Observe not ready; a ruleset changed after observe rejects the promotion push and `main` is unchanged | blocker `branch_protection_incompatible` |
| Protection unreadable | Observe not ready | blocker `main_protection_unverifiable` |
| App bypass still present in train mode | Allowed with a warning | warning `main_protection_app_bypass` |
| Development requested without the bypass | Refused | `main_protection_blocks_development_publisher` |
| Attestation publication fails | The candidate stays unpromoted and the publication lease retries | status `configuration_blocked` |
| Token mint fails (key, permissions, installation) | Every GitHub operation for the project stops; there is no fallback to a login | `app-verify` `credential` fail |
| Break-glass push to `main` | Accepted by GitHub (bypass); full CI runs afterwards | `Main attestation` red, or CI results on `main` |
