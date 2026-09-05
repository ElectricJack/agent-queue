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

GitHub discovery and optional repository creation use the `gh` login on the
machine where the daemon runs. Authenticate there, under the account that should
be able to view, clone, or create repositories:

```bash
gh auth login
gh auth status
```

The wizard can then search accessible repositories and list owners that can
create repositories. Pasting a GitHub URL avoids discovery, but private clones
still need host credentials. AQ never returns, logs, or stores GitHub tokens,
credential-helper output, or `gh auth token` output; subprocess errors are
scrubbed for credential-bearing URLs.

## Choose a mode

The wizard has three source modes:

| Mode | What it does |
| --- | --- |
| **Existing local repository** | Select a valid Git repository below a configured root. AQ records its remote and default branch when available, without fetching, checking out, resetting, committing, or otherwise modifying it. |
| **New repository** | Choose a new, non-existent child directory. AQ initializes Git on `main` by default and can create an initial README and commit (enabled by default). It can also create an optional GitHub repository, private by default. |
| **Clone from GitHub** | Search repositories visible to host `gh`, or paste a GitHub URL or shorthand, then clone into a new destination below a configured root. |

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
| `github_auth_required` | Run `gh auth login` on the daemon host for an account with the needed access, then retry. |
| `github_repository_inaccessible` | Confirm the owner/repository and host account permissions, or paste/select a repository the host can access with a new request ID. |
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
