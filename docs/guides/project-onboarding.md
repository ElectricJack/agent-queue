---
tags: [projects, onboarding, operator, github, workspaces]
---

# Project onboarding

Use the dashboard's **Add project** button to link a repository already on the
daemon host, initialize a new repository, or clone a GitHub repository. The
same server-owned operation is available from the CLI as `aq project onboard`.
It creates the AQ project, its primary `project-repo` workspace, and standard
vault directories as one onboarding operation; do not compose `create_project`
and `add_workspace` for this path.

## Configure project roots

Repositories selected or created through onboarding must be under a configured
root on the daemon host. `project_roots` is separate from `workspace_dir`,
which AQ uses for agent worktrees and is not an allowlist of source trees.

```yaml
project_roots:
  - id: development
    label: Development
    path: /home/aq/dev
  - id: experiments
    label: Experiments
    path: ~/experiments
```

Each `id` is a stable, URL-safe identifier and each `label` is shown to the
operator. Paths may be absolute or home-relative. AQ expands and canonicalizes
them; IDs and canonical paths must be unique. A root must exist and be
readable, and creating or cloning also requires it to be writable.

Edit the configuration through Settings or `aq system config edit`. Root
changes hot-reload through the normal round-trip configuration editor, so a
daemon restart is not required. Run `aq doctor` after changing roots to check
their availability. Removing a root only prevents new browsing and onboarding
there; it does not alter registered projects or workspaces below that root.
The [configuration specification](../specs/config.md) defines the complete
configuration contract.

## Permissions and paths

Browsing roots and onboarding projects are privileged project-management
operations. The daemon, not the browser, enforces the policy. A configured root
grants only the ability to browse and onboard below it; it does not grant a
general file-reading capability.

The picker and CLI submit a root ID plus a relative child path. They do not
authorize arbitrary absolute paths. AQ rejects absolute paths, traversal,
symlink escapes, invalid names, managed `.aq/worktrees` paths, and destinations
already registered to another project. It validates containment again when it
mutates state. Link mode accepts only a valid Git worktree root, and browsing
or linking never changes that repository.

## GitHub on the daemon host

Install GitHub CLI (`gh`) on the daemon host for **either** credential mode;
`aq install` does not install it. Choose one of these setups:

* **Existing login only:** leave `integration.github_app` unset. AQ uses the
  daemon OS user's `gh` credentials, with `GH_TOKEN` before `GITHUB_TOKEN`
  before the stored login. Authenticate that OS user if needed:

  ```bash
  gh auth login
  gh auth status
  ```

  The wizard can search accessible repositories, list owners and optionally
  create a GitHub repository, subject to that login's permissions.
* **GitHub App only:** install the App on the existing repository, configure
  [`integration.github_app`](../reference/configuration.md#github-credentials),
  then restart the daemon. No personal PAT, `gh auth login`, or SSH key is
  required for AQ's supported operations on an already registered GitHub
  repository. Supply an explicit repository URL: AQ checks that the App
  installation can access it. Account-wide search, owner selection, user
  identity, repository creation and profile gists are unavailable in App mode.
  **This staged onboarding flow still refuses an App-backed clone** after URL
  validation;
  link a repository already on the host or use an existing AQ project for
  App-backed delivery. AQ does not switch to a personal login for the clone.

A stored personal login can coexist with an App. AQ selects the configured App
for its GitHub operations and supplies a repository-scoped token to each `gh`
invocation without changing the stored login or daemon environment. If App
binding, minting or access fails, AQ reports that failure; it never retries
with a PAT or SSH key. Changes to the effective App mode, installation or key
reference take effect only after a daemon restart. In-memory token refresh does
not need one.

AQ never returns, logs, or stores GitHub tokens, credential-helper output, or
`gh auth token` output; subprocess errors are scrubbed for credential-bearing
URLs.

## Choose a mode

The wizard has three source modes:

| Mode | What it does |
| --- | --- |
| **Existing local repository** | Select a valid Git repository below a configured root. AQ records its remote and default branch when available, without fetching, checking out, resetting, committing, or otherwise modifying it. |
| **New repository** | Choose a new, non-existent child directory. AQ initializes Git on `main` by default and can create an initial README and commit (enabled by default). It can also create an optional GitHub repository, private by default. |
| **Clone from GitHub** | With an existing login, search repositories or paste a GitHub URL or shorthand, then clone below a configured root. App mode validates an explicit URL but currently refuses the clone. |

The dashboard keeps non-secret values when you move backward or retry. The
review step shows every persistent action before submission, with GitHub
creation called out because it changes external state.

For automation, use the same fields with the CLI command. The exact flags are
shown by `aq project onboard --help`; for example:

```bash
# Link an existing repository chosen relative to a configured root.
aq project onboard --source-mode link --root-id development \
  --relative-path services/inventory --project-name Inventory --project-id inventory \
  --request-id link-inventory-20260905

# Clone a GitHub repository into a new relative destination.
aq project onboard --source-mode github_clone --root-id development \
  --relative-path services/inventory --github-url github:example/inventory \
  --project-name Inventory --project-id inventory --request-id clone-inventory-20260905
```

Use a new request ID when starting a distinct operation. Repeating the same
request ID with the same normalized input is safe and returns the existing
result or progress; reusing it with different input is rejected.

## Bind a repository created after initialization

If a project was initialized without a remote and its GitHub repository was
created later, the local operator or live global supervisor can authorize that
repository explicitly. An origin configured in a checkout alone does not
authorize worker publication. Use the exact current project URL as the expected
value; `''` means the project has no repository binding:

```bash
aq project bind-repository agent-q-sprint-eval \
  --repo-url https://github.com/ElectricJack/agent-q-sprint-eval.git \
  --expected-repo-url '' \
  --reason 'User-authorized Agent Q Sprint Eval: bind the repository created after init'
```

Run this against a daemon containing the binding command, after the normal
authorized delivery of its implementation. Worker tokens cannot perform this
repair. The daemon validates GitHub access with its configured credentials,
then commits the project URL and an operator/reason audit event atomically.
No checkout, pending commit, task assignment or human approval gate changes.
Resume the existing worker's guarded `aq git push` after successful binding;
retain its implementation at `6dda504577f2e21e183d211d2eb2c7c4ef2f7590`.

The command refuses a changed expectation, reassignment of a nonempty URL,
active Git pushes, and configured or durable integration publication state.
Retry `repository_publication_busy` after the push finishes. A stale response
requires reading the current project again. Repeating a successful first-bind
request with the old empty expectation returns stale; a readback followed by
the same canonical URL and matching expected URL returns unchanged success
without a second audit event. This command does not deploy AQ, restart its
daemon, push project code, or release a gate.

Existing supervisor profiles receive the new command capability additively on
normal profile reload/start unless `capability_sync: false` was configured.

## Recover from errors

The wizard preserves non-secret form values, highlights field errors, identifies
the failed phase for operation errors, and offers retry when it is safe. Use the
stable error code to take the matching recovery action:

| Error code | Recovery action |
| --- | --- |
| `project_id_conflict` | Choose an unused project ID, or open the existing project if it is the intended one. Start a new request ID when changing the project ID. |
| `destination_conflict` | Choose a new, non-existent destination; AQ will not merge into or overwrite an existing directory. Start a new request ID when changing the destination. |
| `destination_locked` | Wait for the in-progress onboarding request that owns the destination, then retry with the same request ID. |
| `invalid_git_repository` | In link mode, select the actual root of a valid Git worktree below the configured root. |
| `root_escape` | Choose a relative descendant that resolves inside the selected project root; do not use traversal or a symlink escape. |
| `root_unavailable` | Restore the root's existence, readability, and required write access on the daemon host, then run the doctor check and retry. |
| `github_cli_missing` | Install GitHub CLI on the daemon host and retry. |
| `github_auth_required` | In existing-login mode, run `gh auth login` as the daemon OS user or provide a usable environment token, then retry. |
| `github_operation_unsupported` | In App mode, use an explicit existing repository URL for access checks; account operations and App-backed onboarding clone are unavailable in this staged release. |
| `github_repository_inaccessible` | In App mode, check the App installation and repository selection; in existing-login mode, check the URL and daemon user's access. Retry with a new request ID if inputs change. AQ does not fall back between modes. |
| `github_repository_conflict` | Pick a different GitHub owner or repository name, or use the existing repository through clone/link mode; use a new request ID when changing those inputs. |
| `clone_failed` | Check host network and GitHub access, remove only an AQ-reported request-owned staging directory if recovery asks for it, then retry unchanged with the same request ID. |
| `init_failed` | Check the destination root is writable and the target does not exist, then retry unchanged with the same request ID or use a new ID for a new destination. |
| `commit_failed` | Configure Git author identity on the daemon host or disable the initial README/commit option, then retry. |
| `push_failed` | Check GitHub authorization and remote access; retry after correcting access, noting that a local repository may already exist. |
| `registration_failed` | Retry with the same request ID after resolving the reported database or vault issue; inspect the returned resource summary before taking manual action. |

## What survives a failure

Onboarding uses request-owned staging and bounded cleanup rather than assuming a
global rollback. Linked repositories are never modified, removed, or deleted.
Existing destinations are never overwritten or deleted. For a new or cloned
repository, AQ removes a final directory only when this request created it. A
GitHub repository created during onboarding is retained even if a later local,
database, or vault step fails; the error reports its URL and recovery action.
Do not delete a retained GitHub repository merely to retry unless you have
independently decided it is unwanted.

## After onboarding: the `project.created` event

A request's first success emits one `project.created` event on the daemon's
event bus. It carries `project_id`, `name`, `source` (`onboarding`),
`source_type` (`link`, `init` or `clone`) and the primary workspace's
`workspace_id` and `workspace_path`. It also carries `workspace_in_vault`, which
is true when that workspace is the AQ vault or lies beneath it. Replaying a
finished request returns the stored result and emits nothing.
`create_project` emits the same event with `source: command` and no workspace
fields, because it registers no workspace. The event exists so a system
playbook can apply per-project defaults, for example filing a setup chore. The
event only states facts; whether a vault-hosted project is skipped is up to the
playbook. See [Playbooks V2](../concepts/playbooks.md).
