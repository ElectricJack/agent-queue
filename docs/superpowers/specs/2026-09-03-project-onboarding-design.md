---
tags: [design, dashboard, projects, workspaces, git, github, security]
status: approved
date: 2026-09-03
related:
  - 2026-08-21-dashboard-v2-and-work-pipeline-design.md
  - ../../specs/design/workspaces-v2.md
  - ../../specs/models-and-state-machine.md
---

# Project onboarding from the dashboard

## 1. Outcome

An operator can add a project from the dashboard without leaving the Projects
section or manually coordinating project, workspace, vault, and Git commands.
A compact `+` button beside the left-rail **Projects** heading opens one
onboarding wizard with three source choices:

1. Link an existing local Git repository.
2. Initialize a new local Git repository, optionally create a GitHub remote,
   and optionally create an initial README commit.
3. Clone an existing GitHub repository selected through the daemon host's
   authenticated `gh` session or supplied as a pasted GitHub repository URL.

Every local repository must live beneath a strictly configured project root.
The dashboard never accepts an arbitrary absolute path. Successful onboarding
creates the AQ project, its primary `project-repo` workspace, and its vault
structure, then selects and opens the new project.

## 2. Current state

- `dashboard/src/shell/LeftRail.tsx` renders a collapsible Projects heading and
  project links, but has no creation control.
- `create_project` creates the database project and vault directories. It can
  record a repository URL but does not acquire or attach a repository.
- `add_workspace` separately supports `link`, `clone`, and internally `init`,
  although its public tool schema only advertises `link` and `clone`.
- `RepoSourceType` already distinguishes `LINK`, `INIT`, and `CLONE`.
- The existing `workspace_dir` is the location for AQ-managed agent working
  copies. It is not an allowlist of operator-owned source repository roots.
- The existing workspace file browser is scoped to registered workspaces; it
  must not be widened into a general filesystem browser.
- Project creation followed by workspace creation is currently non-atomic. A
  caller composing the two commands can leave a project without a usable
  primary repository.

The new flow reuses these models and lower-level Git/database/vault operations,
but introduces one orchestration boundary responsible for validation,
preparation, registration, and bounded rollback.

## 3. Decisions

### 3.1 One server-owned onboarding operation

The dashboard submits one `onboard_project` command. A new
`ProjectOnboardingService` owns the workflow and is called by the command
handler. The browser does not compose `create_project`, `add_workspace`, and
Git operations.

The service must use the existing asynchronous Git manager and database/vault
primitives. It is the only component allowed to translate an onboarding
request into filesystem mutations and AQ records. Its interface is independent
of HTTP and dashboard concerns so CLI or other trusted surfaces can reuse it.

Onboarding is synchronous from the caller's perspective for the first release:
the request stays pending while Git finishes and the UI displays the current
phase. A persistent, resumable onboarding-job subsystem is explicitly deferred.
The service boundary must permit that later change without moving validation or
Git logic into the dashboard.

### 3.2 Strict configured project roots

AQ configuration gains a root-level `project_roots` list:

```yaml
project_roots:
  - id: development
    label: Development
    path: /home/jkern/dev
  - id: experiments
    label: Experiments
    path: /home/jkern/experiments
```

Each entry has exactly:

| Field | Contract |
| --- | --- |
| `id` | Stable, URL-safe identifier unique within the configuration. |
| `label` | Operator-facing dropdown label. |
| `path` | Absolute or home-relative directory on the AQ daemon host. |

Configuration loading expands and canonicalizes each path. IDs and canonical
paths must be unique. A root must exist and be readable; creation and cloning
also require it to be writable. Root changes use the existing round-trip config
editor and are hot-reloadable. They do not reuse or change `workspace_dir`.
Removing a root prevents new browsing and onboarding beneath it but does not
disable, rewrite, or delete projects and workspaces that were already
registered there.

The Settings UI exposes add, edit, and remove controls for project roots. The
onboarding wizard only consumes configured roots. If none exist, its empty
state links to the relevant Settings section; it never offers a free-form path
escape hatch.

### 3.3 Paths are root-relative capabilities

The dashboard sends `root_id` plus a relative descendant path. It never sends
an authoritative absolute path. The backend resolves the configured root and
repeats validation for every browse, preflight, and create operation.

Validation must:

- reject absolute child paths, `..`, NUL bytes, and platform-invalid names;
- resolve symlinks and require the real target to remain beneath the selected
  root;
- reject paths that traverse or alias AQ's managed `.aq/worktrees` trees;
- reject a destination already registered to another project;
- use component-aware containment, not string-prefix matching; and
- perform mutation-time validation again after preflight to limit TOCTOU
  exposure.

Operators may navigate through nested child directories. Browse results omit
hidden directories by default and include only the entry name, root-relative
path, directory flag, Git-repository flag, and selection eligibility. A linked
source is selectable only when it is a valid Git worktree root. Merely browsing
or selecting an existing repository never runs mutating Git commands.

### 3.4 Project identity

The wizard derives the display name and project ID from the repository name or
new directory name. Both are editable before submission:

- Display name is human-readable.
- Project ID is a normalized, URL-safe slug and is shown separately.

The UI checks obvious format and collision errors immediately. The service is
authoritative and repeats both checks. Project IDs and final repository paths
must be unique. Existing/cloned repositories supply a detected default branch;
new repositories default to `main`.

Other project settings—including concurrency, budget, assignment playbook, and
default profile—retain the normal project defaults and can be edited after
creation. They do not expand this focused wizard.

## 4. Wizard behavior

### 4.1 Entry point

The Projects disclosure row remains one keyboard-navigable unit. A separate,
properly labeled icon button sits at its right edge:

- accessible name: `Add project`;
- tooltip: `Add project`;
- click opens the wizard without toggling the Projects disclosure; and
- completing the wizard expands Projects even if it was collapsed.

### 4.2 Shared step sequence

1. **Choose source** — Existing local repository, New repository, or Clone from
   GitHub.
2. **Choose repository** — Fields vary by source mode.
3. **Project identity** — Editable display name and project ID; detected or
   default branch is shown.
4. **Options** — Only options relevant to the selected source appear.
5. **Review and create** — Show the normalized operation before mutation.

Back navigation preserves entered values. Switching source mode preserves only
shared project identity values and clears incompatible source-specific values.
Closing the wizard before submission performs no mutation.

### 4.3 Existing local repository

The operator selects a configured root, navigates through child directories,
and selects a valid Git repository root. The review action is labeled
`Link project`.

AQ detects the remote URL when present and records it on the project. A local
repository without a remote is valid. AQ detects its default branch using the
existing Git manager rules. Linking does not fetch, checkout, reset, commit, or
otherwise modify the repository.

### 4.4 New repository

The operator selects a configured root and enters a new child-directory name.
The destination must not exist.

`Create initial README and commit` is enabled by default. When enabled, AQ:

1. initializes Git on the selected default branch;
2. creates a minimal `README.md` containing the project display name only when
   that path did not previously exist; and
3. creates an initial commit.

When disabled, AQ leaves an unborn local repository. If GitHub creation is
also requested, AQ creates the remote but cannot push a branch until a commit
exists; the review screen states this explicitly.

`Create GitHub repository` is disabled by default. When enabled, the operator
chooses an owner available through `gh`, confirms or edits the repository name,
and chooses private or public visibility. Visibility defaults to private. The
selected owner, name, and visibility appear on the final confirmation.

### 4.5 Existing GitHub repository

The operator can either:

- search and select repositories visible through the AQ daemon host's current
  authenticated `gh` session; or
- paste a GitHub HTTPS URL, SSH URL, or accepted GitHub shorthand.

AQ normalizes all forms to a canonical owner/repository identity and validated
clone URL. Shell commands receive argument arrays; repository input is never
interpolated into a shell string. Only GitHub repositories are in scope for
this flow. Generic Git remotes can be considered separately later.

The operator then selects a configured destination root. The repository name
is the default destination directory and project identity, but both remain
editable. The final action is labeled `Clone and add project`.

### 4.6 Success

After success the dashboard:

1. closes the wizard;
2. invalidates project and workspace queries;
3. expands the Projects section;
4. selects the new project; and
5. navigates to its overview.

## 5. API and command contract

### 5.1 Root discovery and browsing

Two read-only commands are added:

- `list_project_roots` returns `id`, `label`, and a display path for each
  configured root, plus readable/writable capability flags.
- `browse_project_root` accepts `root_id` and an optional relative directory;
  it returns the normalized relative directory and safe child entries.

The browse operation has a bounded result count, deterministic ordering, and a
structured `not_found`, `not_directory`, `root_escape`, or `root_unavailable`
failure. It does not follow a symlink outside the root or reveal children of
such a symlink.

### 5.2 GitHub discovery

Read-only GitHub discovery commands use the daemon host's existing `gh`
authentication:

- `get_github_auth_status` reports whether `gh` is installed and authenticated,
  without returning credentials.
- `list_github_owners` returns owners under which the authenticated user can
  create repositories.
- `search_github_repositories` accepts a bounded query and cursor/page input and
  returns repository identity, visibility, clone URLs, and default branch.

A pasted URL does not require discovery, but cloning a private repository still
requires usable host credentials. Missing `gh` or authentication produces a
setup-oriented error rather than a generic process failure.

### 5.3 Onboarding request

`onboard_project` accepts common fields:

```text
request_id
source_mode: link | init | github_clone
root_id
relative_path
project_name
project_id
default_branch
```

Mode-specific fields are a discriminated union:

- `link`: no additional fields;
- `init`: `create_readme`, `create_github`, and, when requested,
  `github_owner`, `github_repo`, `github_visibility`;
- `github_clone`: exactly one of `github_repository` selected by discovery or
  `github_url` supplied directly.

Unknown fields and invalid combinations are rejected. `request_id` is an
idempotency key. Replaying a completed request returns its prior result;
replaying an active request reports its current phase; using the same ID with
different normalized inputs is rejected.

Idempotency is durable, not process-local. Add a `project_onboarding_requests`
table containing the request ID, normalized-input fingerprint, status, current
phase, created-resource ledger, result or scrubbed error, and timestamps. The
ledger contains only identifiers and paths needed for bounded recovery; it
never stores GitHub credentials. Terminal records remain available for a
retention period defined alongside existing operational-event retention rather
than growing without bound.

`get_project_onboarding` accepts a request ID and returns its status, current
phase, safe result, or structured error. The dashboard may poll this command
while the synchronous `onboard_project` request is in flight. Losing the
browser connection does not authorize rollback or deletion; retrying with the
same request ID reads the durable record and either returns the terminal result
or resumes a safely retryable phase.

The success response includes project ID, workspace ID, source type, root ID,
relative path, canonical local path, default branch, optional remote URL, and
the actions performed. Error responses use stable machine-readable codes and a
human-readable message.

The API model change requires regenerating `openapi.json`, the Python client,
and the TypeScript client using the repository's standard scripts.

## 6. Transaction and filesystem semantics

Filesystem, GitHub, vault, and database state cannot share one ACID
transaction. The service therefore implements a deliberate saga with bounded
compensation and ownership tracking.

### 6.1 Preflight

Before mutation, the service:

1. normalizes and validates the request;
2. resolves and revalidates the selected root and destination;
3. verifies project ID and registered-workspace uniqueness;
4. validates source Git state or GitHub access;
5. determines the default branch and remote metadata where applicable; and
6. acquires locks for the request ID, project ID, and canonical destination.

### 6.2 Prepare

For `init` and `github_clone`, work is prepared in a uniquely named hidden
staging directory under the selected root. Keeping staging on the same
filesystem allows an atomic rename into the final destination. AQ records every
resource created by this request; cleanup may touch only those resources.

For `link`, prepare is read-only and records no owned filesystem resource.

### 6.3 External GitHub action

For optional GitHub creation, AQ creates the remote only after local Git
preparation succeeds. If an initial commit exists, it configures `origin` and
pushes the selected default branch. If no commit exists, it configures the
remote without pushing and reports that state as successful and expected.

GitHub repositories are persistent external resources and are never deleted by
automatic compensation. If a later step fails, the response includes the
created repository URL and the safe recovery action.

### 6.4 Publish and register

AQ atomically renames an owned staging directory to its final destination, then
uses one database transaction to insert:

- the `Project` row; and
- one enabled primary `Workspace` row of kind `project-repo`, with source type
  `LINK`, `INIT`, or `CLONE` matching the chosen mode.

It then creates the standard vault project directories. Existing project and
workspace query methods remain the source of truth after registration.

If database or vault registration fails, AQ removes database rows created by
the request and removes the final local directory only when that directory was
created by this request. A linked directory is never removed or changed. A
failure after GitHub creation retains the remote and reports it.

The implementation must survive a repeated request after process interruption:
staging directories carry the request ID, and preflight distinguishes safe
resume/cleanup state from a collision owned by someone else. First-release
recovery may be retry-driven; an autonomous background reaper is deferred.

## 7. Concurrency and security

- An idempotency record prevents duplicate execution after browser retries.
- Per-project-ID and per-canonical-destination locks prevent concurrent
  requests from passing the same preflight.
- All filesystem authorization happens on the daemon, under the existing
  privileged local/global-admin command policy used for project management.
- Configured root membership grants browsing and onboarding capability only;
  it does not grant arbitrary file-read APIs.
- Browser responses expose only root-relative directory structure needed by
  the picker. They do not return file contents.
- GitHub tokens, credential-helper output, and `gh auth token` output are never
  returned, logged, or stored in AQ.
- Subprocess errors are scrubbed for credential-bearing URLs before logging or
  returning them.
- Clone/init destinations must not exist at publish time. AQ never merges into,
  overwrites, or deletes a pre-existing directory.
- README creation uses exclusive creation and cannot replace an existing file.
- User-controlled repository, owner, branch, and path values are passed as
  arguments to async process APIs, never through shell interpolation.

## 8. Errors and recovery UX

Stable error codes cover at least:

- `project_id_conflict`
- `destination_conflict`
- `destination_locked`
- `invalid_git_repository`
- `root_escape`
- `root_unavailable`
- `github_cli_missing`
- `github_auth_required`
- `github_repository_inaccessible`
- `github_repository_conflict`
- `clone_failed`
- `init_failed`
- `commit_failed`
- `push_failed`
- `registration_failed`

The wizard keeps all non-secret form values after a failure and focuses the
error summary. Field errors are attached to their fields; operation errors show
the failed phase and a retry action. If a local directory or GitHub repository
survives, the UI states exactly what exists and does not claim a full rollback.

The final review page shows all persistent actions before the operator submits.
GitHub creation receives extra emphasis because it changes external state.

## 9. Accessibility

- The `+` control is a real button with an accessible name and visible focus.
- Every wizard step is keyboard operable.
- Modal focus is trapped while open and restored to `Add project` on close.
- Step changes and long-running phases are announced through an appropriate
  live region.
- Validation errors are programmatically associated with their fields and an
  error summary receives focus after failed submission.
- The directory browser exposes navigation hierarchy and selection state to
  assistive technology.
- Progress and success/failure states never rely on color alone.

## 10. Verification

### 10.1 Backend

Focused tests cover:

- config parsing, validation, round-trip editing, and hot reload;
- component-aware containment and symlink escapes;
- hidden/worktree-directory filtering and bounded browsing;
- Git URL normalization without shell interpolation;
- project ID, destination, and cross-project workspace collisions;
- each `LINK`, `INIT`, and `CLONE` success path;
- README enabled and disabled, including unborn-repository behavior;
- GitHub private default, explicit public selection, and no-commit behavior;
- idempotent replay and conflicting reuse of a request ID;
- persisted phase/result recovery across a reconstructed service instance;
- concurrency locks around project IDs and canonical destinations;
- cleanup of request-owned staging/final directories;
- preservation of linked repositories and pre-existing files;
- preservation and reporting of a created GitHub remote after later failure;
- SQLite and PostgreSQL parity for any new persistence; and
- authorization boundaries and secret-scrubbed failures.

Git tests use temporary repositories. GitHub tests use a fake `gh` executable
and must not require network access or a real account in CI.

### 10.2 Dashboard

Component and integration tests cover:

- the independent Projects disclosure and `Add project` controls;
- the no-roots Settings link;
- root dropdown and nested directory navigation;
- source-specific steps and clearing incompatible fields;
- derived but editable project name and ID;
- collision and validation display;
- pasted GitHub URLs and discovery selection;
- private visibility default;
- optional README and optional GitHub creation;
- review-page action summaries;
- retry behavior with preserved values;
- query invalidation, Projects expansion, and navigation after success; and
- keyboard, focus, labeling, and live-region behavior.

### 10.3 Contract and documentation

- Regenerate and verify OpenAPI and both generated clients.
- Document `project_roots`, permissions, GitHub host authentication, and
  recovery behavior in the operator guide.
- Keep `RepoSourceType`, workspace-kind, project, and database specifications
  synchronized with the command contract.
- Run Ruff only on changed Python files, focused backend area tests, dashboard
  unit tests, dashboard typecheck, and dashboard lint. Full-suite execution is
  left to CI under the repository's resource-gating rules.

## 11. Acceptance criteria

1. A compact `Add project` button beside Projects opens the wizard without
   toggling the Projects disclosure.
2. The wizard offers existing local, new local, and existing GitHub sources.
3. All local paths are chosen beneath configured roots and enforced again by
   the backend; no arbitrary path input or traversal works.
4. Operators can navigate nested child folders and select only valid Git
   repository roots for linking.
5. New repositories use `main` by default and offer an optional README/initial
   commit enabled by default.
6. New repositories can optionally create a GitHub repository through the
   host's authenticated `gh`; visibility defaults to private.
7. Existing GitHub repositories can be discovered through `gh` or supplied as
   pasted GitHub links and cloned beneath the selected root.
8. Project display name and ID are derived, editable, and collision-checked.
9. Successful onboarding creates exactly one AQ project, one enabled primary
   `project-repo` workspace, and the standard vault project structure.
10. Failure never modifies or deletes a linked repository or pre-existing
    destination and never silently deletes a GitHub repository.
11. Repeated or concurrent submissions do not create duplicate projects or
    repositories.
12. Success refreshes the left rail, expands Projects, selects the project, and
    opens its overview.
13. Focused backend, dashboard, contract, accessibility, and documentation
    checks pass.

## 12. Alternatives considered

### Dashboard composition of existing commands

Rejected. It reuses more surface code but can leave partial project state,
duplicates validation, and puts security-sensitive sequencing and rollback in
the browser.

### Persistent onboarding jobs in the first release

Deferred. Durable progress, cancellation, and autonomous recovery are valuable
for very large clones, but add a new lifecycle and persistence subsystem. The
service introduced here is the seam for a later job runner.

### Arbitrary paths with a warning

Rejected. The user selected strict configured roots. Operators add a root in
Settings or configuration before it can be browsed or targeted.

### Browser-native directory picker

Rejected. It selects paths on the browser's machine rather than necessarily the
AQ daemon host and cannot enforce daemon-side repository authorization.

## 13. Implementation task graph

> **Status:** the implementation epic has been created in agent queue as
> `brisk-beacon`, with its work packages as sibling children.

After written-spec approval, create one implementation epic with these sibling
work packages:

1. **Project-root configuration and secure browser** — config model/editor,
   settings controls, read-only commands/API, containment tests, and operator
   documentation.
2. **Transactional onboarding core** — request/response models, idempotency and
   locks, service saga, `LINK`/`INIT` modes, database/vault integration, and
   rollback tests. Depends on package 1.
3. **GitHub onboarding** — auth status, owner/repository discovery, URL
   normalization, clone/create/push behavior, secret scrubbing, and fake-`gh`
   tests. Depends on packages 1 and 2.
4. **Dashboard wizard** — left-rail button, modal steps, directory browser,
   GitHub selection/paste flow, review/error/success behavior, and accessibility
   tests. Depends on the API contracts from packages 1–3.
5. **Cross-layer integration and final verification** — generated clients,
   end-to-end command/dashboard scenarios, spec synchronization, and focused
   final gates. Depends on packages 1–4.

Packages are siblings under the epic. Work discovered from a package is also a
sibling under that epic unless it is strictly internal decomposition of the
owning package.
