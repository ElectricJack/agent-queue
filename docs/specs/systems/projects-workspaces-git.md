---
tags: [spec, system, projects-workspaces-git]
system: 6
status: current
as_of: 2026-10-22 (dev 52d2efe4c)
---

# System 6: projects-workspaces-git

This is a **current** system spec, not a design record. It describes what the
code at `dev` 52d2efe4c does. Where it and the code disagree, the code is
right and this file should be fixed. The older specs it links to, such as
[`../git.md`](../git.md) and [`../design/workspaces-v2.md`](../design/workspaces-v2.md),
are kept for the reasoning behind the design, not as a description of what
runs today ([`../README.md`](../README.md) carries the same rule for every
older spec).

projects-workspaces-git is system 6 of the 18 in the system inventory
(`~/.agent-queue/vault/projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, *approved*, revision 1). The inventory merged three
candidate systems into this one: projects and repositories, workspaces, and
"GitHub access, git identity, retained objects", under the rule **one
`GitManager`, one `gh` path**. Sibling specs live in the same directory as
`<system-slug>.md`.

Two pages already describe this system for its users, and this spec does not
repeat them:

- [Projects and workspaces](../../concepts/projects-and-workspaces.md) is the
  current-behaviour concept page: vocabulary, who owns which state, the slot
  lifecycle and the common failures.
- [The workspaces module catalog](../../reference/modules/workspaces.md) lists
  every module in the shard with its responsibility.

This spec adds what neither page has: how the pieces fit together end to end,
where the boundaries with other systems sit, the invariants the code
protects, the gaps, and the planned work.

## 1. Purpose and problem

Agent Queue runs many coding agents at once against the same repositories.
Each agent needs a directory it can write to, on the right branch, that no
other agent is writing to, and that holds nothing left over from the last
task. Each agent's work has to reach GitHub under a known identity and with a
credential that cannot leak into the agent's own environment. And the
repository the daemon keeps for itself, the clone it merges and publishes
from, has to stay intact, because a corrupt object there silently breaks
every later integration.

Done by hand, each of those is a race or a leak. Two agents claim the same
checkout. A reset throws away a crashed agent's unpushed commits. A slot
directory is deleted while a job still runs in it. An ambient `GH_TOKEN` in
the daemon's environment publishes as the operator. A `git gc` started by one
command detaches and outlives the process that started it.

projects-workspaces-git owns all of that. It:

- **registers projects** and their repositories, and onboards new ones
  (link an existing checkout, `git init` a new one, or clone from GitHub);
- **manages workspaces**: the base checkout of each repository and the git
  worktree *slots* cut from it, which agents and jobs lock, use and give
  back;
- **runs every git and GitHub call** through one wrapper, `GitManager`,
  and one `gh` runner, with owned processes, bounded timeouts, controlled
  credentials and an explicit identity;
- **guards the integrity** of the retained object stores the integration
  service merges from, and detects corruption before an object is lent or a
  tip is advertised.

It does not decide *what* is merged or delivered. That belongs to
[integration-and-delivery](integration-and-delivery.md), which calls this
system for every git operation it performs.

## 2. How it works today

The concept page's [vocabulary](../../concepts/projects-and-workspaces.md#vocabulary)
defines project, repository, workspace, base, slot and kind. This section
follows the code.

### 2.1 Where the code lives

| Area | Paths |
|---|---|
| Projects and onboarding | `src/projects/` (including `onboarding.py`, `roots.py`, `relocation.py`, `storage_binding.py`), `src/commands/project_commands.py`, `project_relocation_commands.py`, `src/cli/projects.py` |
| Git and GitHub | `src/git/` (`manager.py`, `github.py`, `github_cli.py`, `identity.py`, `github_app.py`, `ci_gate.py`, `askpass_broker.py`, `github_auth.py`, `locks.py`, `process.py`, `github_contracts.py`, `askpass_fd.py`) |
| Workspaces in the orchestrator | `src/orchestrator/workspace.py`, `worktree_manager.py`, `workspace_attachments.py`, `workspace_claim_recovery.py`, `base_workspace.py`, `retained_workspace.py`, `slot_restore.py`, `workspace_binding_audit.py`, `git_ops.py` (see §3.1) |
| Workspace kinds | `src/profiles/workspace_kind_parser.py`, `workspace_kind_registry.py` |
| Queries | `src/database/queries/workspace_queries.py`, `retained_workspace_queries.py`, project queries |
| Diagnostics and reuse | `src/workspace_listing.py`, `workspace_registration.py`, `workspace_disable.py`, `warm_workspaces.py`, `build_cache.py` |
| Small helpers | `src/workspace_names.py` (`ws-{adj}-{noun}` ids), `src/workspace_spec_watcher.py` (§2.10) |

Runtime repository operations use `GitManager`; GitHub operations use the
startup-owned `GitHubAccess` and its shared `GhRunner`.

### 2.2 Data model

All tables are in `src/database/tables.py`. Persistence and migrations are
owned by [daemon-core](daemon-core.md); this system owns what the rows mean.

**`projects`** (`tables.py`). One row per project. Besides scheduling and
budget fields (`credit_weight`, `max_concurrent_agents` default 2, `status`
ACTIVE / PAUSED / ARCHIVED, where PAUSED is skipped by the scheduler), it
holds the repository binding (`repo_url`, `repo_default_branch`,
`workspace_path`, `storage_binding`), delivery policy (`promotion_flow`,
`default_branch_cutover`, `integration_mode` and the
`hierarchical_integration_*` fields, `integration_repository_id`), routing
(`assignment_playbook_id`, NOT NULL, default `default-assignment-routing`),
Discord channel ids, and an optional git identity override
(`git_identity_name` / `git_identity_email`, where the named constraint
`ck_projects_git_identity_pair` requires both or neither).

**`repos`** (`tables.py`). One project can have several repositories:
`url`, `default_branch`, `checkout_base_path`, and `source_type`
(`RepoSourceType`: CLONE, LINK, INIT, WORKTREE) with `source_path`.

**`project_onboarding_requests`** (`tables.py`). The idempotency record
for onboarding: `request_id`, an `input_fingerprint`, `status`
(pending / succeeded / failed), `phase`, `created_resources`, `result` and
`error`. It holds no credentials and has no foreign key to `projects`, so a
failed request survives without a project.

**`workspaces`** (`tables.py`). One row per directory an agent can be
given. Identity: `id`, `project_id`, `workspace_path` (unique per project),
`source_type`, `name`, `kind_id`. Lock state: `locked_by_agent_id`,
`locked_by_task_id`, `locked_at`, `lock_mode`, and a `generation` counter that
increments on every acquisition. Liveness: `job_pin_count`. Health:
`enabled`, `disabled_reason` (`"<code>: <detail>"`), `disabled_at`. A slot
also carries `slot_index` and `base_workspace_id`, a soft reference to its
base; the partial unique index `uq_workspaces_base_slot` keeps slot numbers
unique per base.

**`workspace_kinds`** (`tables.py`). The primary key is
`(project_id, id)`, and the sentinel project `__system__` holds built-in
kinds. A kind says whether a workspace is `writable`, `lockable`,
`is_git_repo`, its `repo_url`, `default_lock_mode`, whether it is attached to
every task (`auto_attach`), its `mode` (`worktree`, `exclusive-clone` or
`directory-isolated`), `worktree_setup` commands, and `max_slots`.

Related rows elsewhere: `task_workspace_requirements` (which kinds a task
needs, keyed `(task_id, kind_id, position)` with an alias),
`job_workspace_pins` (`tables.py`) and `jobs.workspace_id` /
`jobs.workspace_generation`, `merge_slots` (one per project, leased),
`branch_retirements` (`tables.py`), and task columns
`preferred_workspace_id`, `attachments` and `workspace_mode`. Profiles carry
`needs_workspace` and `allow_base_checkout`; sessions carry
`claim_failure_count` and `quarantined_at`.

Lock modes are `WorkspaceMode` values: `exclusive`, the deprecated alias
`branch-isolated` (treated as exclusive), and `directory-isolated`, a stub
that raises `RuntimeError` if used.

### 2.3 Projects and onboarding

`ProjectOnboardingService.onboard_project` (`src/projects/onboarding.py`)
is the one path that creates a project with a repository. It runs these steps
in order:

1. **Validate and authorize.** The caller must be the operator or a
   supervisor.
2. **Idempotency.** The request id and a SHA-256 fingerprint of the inputs
   are recorded. A replay with the same id returns the stored result or
   error; a replay with different inputs is refused.
3. **Lock** non-blockingly, in process, on the request and the target.
4. **Preflight** the root, the target path and, for GitHub, the owner and
   repository.
5. **Do the mode's work** in a staging directory: `link` an existing
   checkout, `init` a new repository, or `github_clone` (create or reuse a
   GitHub repository, then clone it).
6. **Publish** the staging directory to its final path by rename.
7. **Register** the project and its `<id>-primary` workspace (kind
   `project-repo`) in one transaction.
8. **Bind** the GitHub repository with a compare-and-set.
9. **Push** the initial branch, create project storage, and emit
   `project.created` on the first success only (`src/projects/events.py`; the
   event carries facts only, and a bus failure is swallowed).
10. **On failure, compensate** (`_compensate`, `onboarding.py`): remove
    only the directories whose owner marker this request wrote, never delete
    a GitHub repository, and return a `recovery_command` for the operator.

Paths are validated in `src/projects/paths.py`: every path is relative to a
configured project root, symlinks may not escape it, and `.aq/worktrees` is
refused both as written and after resolution. A `ProjectPathError` never
reveals the resolved path. `src/projects/roots.py` assesses aggregate root
readiness as OK, MISSING, UNREADABLE or UNUSABLE for doctor and installer
surfaces; `aq project list-roots` lists the configured roots.

The operator guide is [project onboarding](../../guides/project-onboarding.md),
and the design record is
[`2026-09-03-project-onboarding-design.md`](../../superpowers/specs/2026-09-03-project-onboarding-design.md).

After onboarding, `src/commands/project_commands.py` guards the binding:

- `bind_project_repository` is a first-binding compare-and-set under an
  advisory lock and `FOR UPDATE`. It refuses a stale request, an
  `already_bound` project, `publication_busy` and `integration_active`.
- Editing a project refuses a `repo_url` change. The routing binding can
  never be unbound.
- Deleting a project needs integration disabled and drained and no
  IN_PROGRESS task, and the delete query itself refuses with
  `integration_history_retained`.

The hand-written CLI verbs are in `src/cli/projects.py` (`create`, `onboard`,
`bind-repository`, `details`, `set`). The rest of `aq project …`, including
every workspace verb, is generated from command contracts by
`src/cli/auto_commands.py`. There is no `aq workspace` group; workspace verbs
are `aq project workspace-doctor`, `aq project workspace-reap`, `aq project
release-workspace` and their siblings.

### 2.4 The workspace lifecycle

[The slot lifecycle](../../concepts/projects-and-workspaces.md#slot-lifecycle-resume-and-preservation)
on the concept page describes the states. This is the code path through them.

**Bases are registered, slots are grown.** An operator registers a base
workspace (onboarding does it for the primary checkout). Slots are created
lazily: `WorktreeSlotManager.create_slot` (`worktree_manager.py`) names
the slot `{base_id}:slot-N`, provisions it (an exclude block written under a
file lock, `worktree prune`, `fetch`, `worktree add --detach`, then the
kind's `worktree_setup` commands), writes the `.aq-worktree.json` sentinel and
emits `worktree.created`. At most one slot is added per dispatch, up to
`max(1, max_concurrent_agents)` and the kind's `max_slots`.

**Acquire.** When the scheduler launches a task, it calls
`_prepare_workspace` (`workspace.py`). Under the project lock,
`_prepare_workspace_locked` prefers a slot already on the task's branch, then
grows a slot if the cap allows, then acquires through
`workspace_attachments.acquire_for_task`. If nothing is free it returns a wait
reason (`slot_lost_race`, `slot_stalled`, `slot_warming` or `slots_full`) and
the task stays in the frontier. The lock itself is one atomic `UPDATE … WHERE
unlocked AND unpinned` in `acquire_one_unlocked`
(`workspace_queries.py`), which also increments `generation`.

**Reset for the task.** `reset_slot_for_task` (`worktree_manager.py`)
first refuses a slot whose sentinel names another owner (`ForeignSlotClaim`).
With `worktrees.salvage_dirty: true` (the default), it attempts to capture
unpushed commits and dirty work into task context (`worktree_salvage`) before
reset. This is best effort: above `salvage_max_bytes` (default 5 MiB), the
record contains a diff stat rather than file contents; capture failures are
logged and return false. It then uses `reset --hard`, `clean -fd -e .aq-worktree.json`
(never `-x`, so ignored build output survives) and checks out `aq/<task_id>`.
A missing task branch can be recreated at the existing HEAD only when it
exactly matches that task's recorded integration or pause checkpoint, with
no divergent remote ref or conflicting retained binding. No files are reset
by this recovery. Legacy full clones use the `.agent-queue-lock` sentinel
and the older `workspace.py` path.

**Hand off to the session.** The work directory and the claim file
(`.aq/claim.json`, helpers in `src/claim_file.py`) go to
[sessions-and-harnesses](sessions-and-harnesses.md), which starts the agent
there.

**Release.** A release is refused while a job pins the workspace (the
`unpinned_workspace()` guard). After the task, slots go through
`restore_slot_after_task` (`worktree_manager.py`); legacy clones go
through a commit → stash → force-clean → checkout-default ladder.

**Reap.** Slots are deleted only by `reap_slot` (`worktree_manager.py`),
and only above the cap. It needs an exact identity match under a row lock,
an unlocked row with no live session, no integration owner and no claim file,
a successful `/proc` scan showing no process inside the directory (it fails
closed if the scan fails), and zero dirty paths. The scheduler's
`_reap_worktree_slots` (`core.py`) breaks expired merge-slot leases every
cycle and, every five minutes, audits slot bindings
(`workspace_binding_audit.py`), sweeps out-of-cap slots and runs
`restore_disabled_slots`. `prune_branches` and `adopt_existing` (adoption
never deletes) round out the manager.

**Quarantine.** Three identical claim refusals in a row
(`MAX_IDENTICAL_CLAIM_FAILURES = 3`, `src/pool_claims.py`) stop the session
and disable its workspace with reason `claim_failures`. A foreign slot claim
disables the slot at once. `slot_restore.py` is the reconciler that brings
slots back: after a 15-minute cooldown it skips operator-disabled slots and
slots with live processes, re-creates missing directories, re-enables clean
slots, and marks dirty ones `quarantined_dirty`, emitting
`worktree.quarantined` and messaging `supervisor-{project}`.

**Fenced recovery.** A slot whose claim outlived its session is recovered
through `workspace_claim_recovery.py`: `retire_stopped_slot_claim`,
`retire_paused_slot_claim` and `retire_stale_slot_claim`. Each proves its
case under `FOR UPDATE` and removes the claim file only if it is still
byte-identical to what was proved. `retained_workspace.py` finishes an
explicitly requested stop the same way, validating the claim file and the
slot sentinel against the task, workspace and session before it captures a
checkpoint. The operator-facing recovery list is in
[common failures and recovery](../../concepts/projects-and-workspaces.md#common-failures-and-recovery).

**Live pins.** A job that runs in a workspace (see
[test-service-and-jobs](test-service-and-jobs.md)) pins it. `create_job`
re-checks the workspace generation and lock owner, inserts a
`job_workspace_pins` row and increments `job_pin_count`. Lock, delete and
release all refuse while the count is non-zero.

**The base checkout.** Agents work in slots, not in the base.
`base_checkout_refusal` (`base_workspace.py`), added after the 2026-09-01
incident, refuses a session in a base checkout unless its profile sets
`allow_base_checkout`.

The rollout switch is `worktrees.enabled`. With it off, every git kind is
treated as `exclusive-clone` and the legacy clone path runs. Pool routing by
primary workspace kind is the
[hybrid workspace pools](../design/hybrid-workspace-pools.md) design,
implemented in commits 01089082b and 69a45b259. The switch defaults to true;
the kind's `mode` selects steady-state provisioning. Upgrade reconciliation
preserves an existing `exclusive-clone` mode rather than silently converting
it to slots.

`aq project list-workspaces` adds bounded owner, presence and Git diagnostics.
Its diagnostic deadline defaults to 10 seconds (clamped to 0.5–20 seconds);
`--no-git` skips Git probes. Read `diagnostics.complete` and nullable fields:
unknown or partial evidence does not establish that a workspace is free.
`release-workspace` releases a database lock; claim retirement and terminal
sentinel clearing are separate guarded recovery commands.

### 2.5 Workspace kinds and multi-kind attachments

A task can need more than one workspace: its repository and, for example, the
vault. `effective_requirements` (`workspace_attachments.py`) builds the
list from the task's `task_workspace_requirements` rows, or synthesizes a
single `project-repo` requirement when there are none, then appends every
`auto_attach` kind at position 10000 and above and sorts the list into a
canonical lock order so two tasks never deadlock. `acquire_for_task`
(`workspace_attachments.py`) is all-or-nothing: a missing auto-attach
kind is skipped, a missing explicit kind raises `AcquisitionFailed`, and on
any failure every lock taken so far is released.

Kinds are authored as markdown under
`vault/[projects/<pid>/]workspace-kinds/<id>.md` and parsed by
`workspace_kind_parser.py`; a kind file without `mode` keeps the database's
value. `WorkspaceKindStore` scans, bootstraps the built-ins and prunes
removed kinds once, at daemon start. There is no live watcher; a malformed
present file preserves its last valid row during reconciliation.

### 2.6 GitManager: the one git wrapper

`GitManager` (`src/git/manager.py`) wraps runtime repository operations. The
`a`-prefixed async methods are the production API; the synchronous ones
remain for back-compat and tests.

- **Owned processes.** Every child runs through `run_owned_process`
  (`src/git/process.py`) in its own session. The timeout includes spawn time.
  The process group is killed after the child exits, even on exit code 0, so
  nothing it forked survives. If cleanup does not finish in five seconds it
  raises `ProcessCleanupError`, which tells the caller the slot must not be
  reused.
- **No background maintenance.** Foreground git runs with
  `maintenance.auto=false` and `gc.auto=0`, because automatic maintenance can
  `setsid` out of the process group.
- **No prompts.** The base environment sets `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false` and `GH_PROMPT_DISABLED=1`.
- **Timeouts.** 120 seconds by default, 600 for PR diffs, 30 for `gh`.
- **Locking.** `set_lock_provider` and `arepository_transaction`
  (`manager.py`) serialize work per repository in memory; the
  provider lock is taken only for fetch, gc and pull. `src/git/locks.py`
  holds no asyncio locks: it recovers stale `index.lock` and similar files,
  conservatively, after proving no git process owns them.

`git_ops.py` is not a git wrapper despite its name. `GitOpsMixin` is the
orchestrator's completion pipeline (verify, merge and push, create PR, and
`_phase_integrate` under the merge slot), and its behaviour is specified by
integration-and-delivery (§3.1).

### 2.7 GitHub access and credentials

The design is [GitHub access](../github-access.md), delivered by the epics in
[`../../plans/github-access/README.md`](../../plans/github-access/README.md)
(#615, completed 2026-09-23). In code, the layers are:

- `github_contracts.py`: pure types, including the two credential modes, APP
  (a GitHub App installation) and EXISTING_LOGIN (the host's `gh` login).
- `github_app.py` (`AppTokenProvider`): the only direct HTTP to GitHub. It
  allows `GET /app` and the installation `access_tokens` POST and nothing
  else, signs an RS256 JWT, checks that the granted permissions match
  exactly, and opens the private key with `O_NOFOLLOW`, requiring the
  daemon's uid as owner and owner-only permissions.
- `github_auth.py`: a configured App is authoritative, with no fallback to
  ambient tokens. Credentials are cached per (identity, repository) and
  refreshed 300 seconds before expiry.
- `github_cli.py` (`GhRunner`): the one `gh` path. It strips ambient token
  variables, controls `--hostname github.com` and `--repo` itself, sends
  request bodies on stdin so tokens never reach argv, and in App mode passes
  `GH_TOKEN` with a throwaway `GH_CONFIG_DIR`.
- `github.py`: `GitHubAccess` binds each repository and re-verifies the
  binding after 600 seconds. It refuses repository creation and gists in App
  mode (`github_operation_unsupported`). `run_read` retries once after a
  rejection; `run_write` never replays. `GitHubClient` allows a fixed list
  of endpoints.
- Git transport runs `/usr/bin/git` directly. In App mode it runs from a
  throwaway repository, and the credential reaches git through the askpass
  file-descriptor broker (`askpass_broker.py`, `askpass_fd.py`): the broker
  checks `SCM_CREDENTIALS` on the socket, pins the askpass executable, and
  releases the token once and zeroes it.

`aq integration app-verify` (with `--repository-access-only` for a quick
check) proves the App can reach a repository.

### 2.8 Identity, publish policy and the CI gate

**Identity** (`src/git/identity.py`, spec [git identity](../git-identity.md)).
Commits are authored as the project override, else the config
`git_identity:`, else the fallback `Agent Queue <agent-queue@localhost>`. The
integration ledger uses its own `LEDGER_IDENTITY` (`Agent Queue
<aq@localhost>`). Identity is injected through `GIT_AUTHOR_*` and
`GIT_COMMITTER_*` environment variables, never written into git config, so
an agent's checkout cannot change it. `PublishPolicy` checks the committer
digest on publishable commits against
the allowed launch/config identities; author mismatches produce warnings.
Already published, inherited, prerequisite and source-repair commits have
specific exclusions. Changing identity retires idle pool sessions with stale
identity before their next claim.

The internal git plugin guards worker publication before obtaining credentials:
the task must own the branch and repository (or have an explicit active batch
repair delegation). Workers use `aq git push`, and the integration publisher
owns the target branch. Force publication uses an explicit observed remote OID
lease. The low-level `GitManager` is also used by trusted publishers, so this
worker ownership boundary belongs to the command/plugin layer.

PR creation binds to the published head. A matching open PR is reused; a
matching closed, unmerged PR may be reopened and verified; a merged PR is
reused only for the matching published commit. New commits on a reused branch
name require a new PR. Delivery lookup similarly proves that a merged PR's
head contains the current branch tip before treating it as delivered.

**CI gate** (`src/git/ci_gate.py`). Pure logic, added after PR #341 was
merged while red. Each check is classified PASS, FAIL, PENDING or
INCONCLUSIVE; the rollup is GREEN, RED, PENDING or UNKNOWN; and
`integration.merge_ci_policy` (off / warn / required) decides what a merge
does with it. `classify_base` reports how fresh the base is. The merge
decision itself belongs to integration-and-delivery.

### 2.9 Retained object integrity

The integration service keeps retained clones that it merges and publishes
from. A corrupt object there is invisible until a later fetch, merge or push
fails, so this system checks before trusting the store.

- **Detection.** `_averify_retained_objects` (`manager.py`) runs a full
  `git fsck` before an object is lent to a workspace or a tip is advertised.
  A connectivity-only check is not enough. A clean result is cached in
  memory, per process, keyed by a fingerprint of the object store, for one
  hour. Filesystem integrity walks run off the event loop. Retained fetches
  use normal negotiated incremental transfers; full-pack retention is not
  the default.
- **No background maintenance.** Retained stores set `gc.auto=0`,
  `gc.autoDetach=false` and `maintenance.auto=false`, and maintenance runs in
  the foreground when it is asked for
  ([integration repository recovery](../design/integration-repository-recovery.md),
  [retained object integrity](../design/retained-object-integrity.md)).
- **Recovery** is executed by integration-and-delivery
  (`src/integration/repository_recovery.py`, incidents in
  `repository_incidents.py`). It makes at most two attempts, journalled in
  `aq-empty-object-backups/` (`incident.json`, `manifest.json`, `pending`).
  Bad loose objects are moved aside, never deleted; the store is refetched;
  local-only `aq/*` heads are pushed; and a final full fsck must pass. Pack
  corruption always goes to the supervisor. While an incident is open, a
  `system_config` admission hold pauses batches for that project only. When
  attempts run out it escalates as `escalation-retained-<id>`.
- **Clearing.** `aq integration clear-retained-recovery` (needs
  `integration_operator`) requires a clean fsck.

The operator runbook is [retained git corruption](../../guides/retained-git-corruption.md).

### 2.10 The workspace spec watcher

`src/workspace_spec_watcher.py` is unrelated to workspace kinds despite the
name. It watches spec and documentation files in project checkouts (by mtime
or `git diff`, polling every 60 seconds), writes reference stubs to
`vault/projects/{id}/references/` (`spec-` or `doc-` names derived by
`derive_stub_name`), and emits
`workspace.spec.changed`. See
[how workspace reference stubs fit in](../../concepts/projects-and-workspaces.md#how-workspace-reference-stubs-fit-in).

### 2.11 Native storage relocation

`aq project relocate` is a local/operator-only preview/apply command backed
by `src/projects/relocation.py`. The project must be paused, its integration
train disabled and drained, and its sessions, claims, workspace locks, jobs,
pins and integration owners quiescent. The operator supplies an inventory of
external services and proves they are stopped; an empty inventory must be
explicit. Native destination validation rejects Windows-mounted storage and
unsafe Git layouts.

Preview inventories repository and workspace bytes, ignored files, Git refs,
retained stores, recovery data and configured compiler caches, returning a
plan digest and blockers. Apply requires that digest, makes independently
verified copies and backups, runs the project's pinned required checks, then
rechecks ownership, policy, content and process fences before atomically
publishing a new `projects.storage_binding` generation and the associated
repository/workspace paths. It does not edit daemon configuration or enable
the train. Reverse relocation is another verified forward copy to a fresh
destination, never a switch back to stale source bytes. See the
[storage relocation runbook](../../guides/matter-engine-storage-relocation.md)
for exact flags and receipt handling; the runbook records fixture validation,
not a claim that the live project has moved.

### 2.12 Warm workspaces and compiler caches

`src/warm_workspaces.py` supplies reusable linked checkouts off the primary
object store for integration close-merge, fixer and job snapshot callers.
Owner manifests live outside the checkout. Callers serialize allocation and
prove the previous writer stopped, pins were released and committed work was
preserved before reuse. Same-owner replay preserves files for reconciliation;
dirty, foreign or ambiguous preparations are retained. Reuse checks out the
new commit with `--no-overwrite-ignore` and resets tracked files without
`clean -fdx`, preserving ignored build output when it is safe.

The optional `workspaces.build_cache` configuration selects `ccache` or
`sccache` and an absolute native Linux `dir`; it is unset by default.
`workspaces.projects.<project-id>` overrides it, including explicit null to
disable it. `src/build_cache.py` resolves relocated cache paths and supplies
`CCACHE_DIR` or `SCCACHE_DIR` to job and session environments. Bounded cache
telemetry reports shared-cache counters; missing tools yield unknown
measurements, not a build failure. Configuration examples are in
[configuration](../../reference/configuration.md#warm-integration-checkouts-and-compiler-caches).

## 3. Boundaries and interactions

### 3.1 Ownership inside shared packages

`src/orchestrator/` is split by file across systems 1, 3 and 6, and
[daemon-core §3.1](daemon-core.md#31-who-owns-srcorchestrator) is the table
of record. This system owns `worktree_manager.py`, `workspace.py`,
`workspace_claim_recovery.py`, `workspace_attachments.py`, `git_ops.py`,
`base_workspace.py`, `retained_workspace.py`, `slot_restore.py` and
`workspace_binding_audit.py`. Three rules apply across that split:

- **daemon-core owns ordering.** When `_reap_worktree_slots`, the binding
  audit and the slot restore run in `run_one_cycle`, and how their failures
  are isolated, is daemon-core's. Their behaviour is this system's.
- **`git_ops.py` is split by responsibility.** The file sits here, but the
  completion pipeline it implements (what to verify, when to merge, when to
  open a PR) is specified by integration-and-delivery. This system owns only
  the git calls it makes, which all go through `GitManager`.
- **`src/integration/` uses this system, it does not own it.** Retained-store
  recovery, incidents, batches, completion provenance
  ([git completion provenance](../design/git-completion-provenance.md)) and
  branch retirement execution
  ([branch retirement](../design/branch-retirement.md)) are integration's.
  The detection primitive (§2.9) and the git operations underneath them are
  here.

### 3.2 Interactions with the other systems

| System | Interaction |
|---|---|
| [1 daemon-core](daemon-core.md) | Owns the schema, migrations and `run_one_cycle` order. Builds `WorkspaceKindStore` at start. Every project and workspace change is a command through `CommandHandler`. |
| [2 task-graph](task-graph.md) | Tasks carry `preferred_workspace_id`, `workspace_mode`, `attachments` and `task_workspace_requirements`; this system reads them. Branch names `aq/<task_id>` follow task ids. |
| [3 scheduling-and-claims](scheduling-and-claims.md) | Owns the frontier, pools, `claim_preparation`, claim queries and claim-failure counting. It calls `_prepare_workspace` and acts on its wait reasons; this system returns a locked workspace or a reason. The quarantine threshold is counted there and enforced here (disable with `claim_failures`). |
| [4 routing-and-profiles](routing-and-profiles.md) | Owns profiles; this system reads `needs_workspace` and `allow_base_checkout`. Hybrid pools route by primary workspace kind. |
| [5 sessions-and-harnesses](sessions-and-harnesses.md) | Receives the work directory and the claim file, starts the tmux session there. `src/sessions/proctable.py` supplies the `/proc` liveness scan used by reap and retained stops. |
| [7 integration-and-delivery](integration-and-delivery.md) | The heaviest caller of `GitManager`. Owns merge policy, batches, retained-store recovery and branch retirement; uses the CI gate, identity, publish policy and the GitHub layers from here. |
| [10 supervisors-and-policy](supervisors-and-policy.md) | Receives `worktree.quarantined` messages, pack-corruption and retained-recovery escalations. Supervisors may onboard projects. |
| [13 configuration-and-vault](configuration-and-vault.md) | Owns `worktrees.enabled`, `git_identity`, project roots, the GitHub App configuration and the vault layout that holds workspace-kind files. |
| [14 api-and-clients](api-and-clients.md) | Generates the `aq project …` workspace verbs and their API routes from command contracts. |
| [16 test-service-and-jobs](test-service-and-jobs.md) | Jobs pin workspaces; this system refuses to lock, release or delete a pinned one. |
| [17 operations-and-recovery](operations-and-recovery.md) | Owns `aq doctor`; the checks `worktrees.orphans`, `worktrees.capacity` and `projects.roots` read this system's state. |
| [18 plugins-mcp-and-tools](plugins-mcp-and-tools.md) | The internal git plugin (`src/plugins/internal/git.py`) routes GitHub calls through `GhRunner`. |

## 4. Design principles and invariants

1. **One wrapper, one `gh` path.** No production code spawns `git` or `gh`
   except through `GitManager` and `GhRunner`, using the async API. This is
   what makes timeouts, process cleanup, identity and credentials
   enforceable.
2. **Nothing a git command starts outlives it.** Owned process groups are
   killed after exit, and automatic maintenance is off. A cleanup failure
   retires the slot rather than reusing it.
3. **Credentials never reach the agent or argv.** Ambient tokens are
   stripped, App tokens travel by stdin or the askpass broker, and a
   configured App is authoritative with no fallback.
4. **Identity is injected, not configured.** Author and committer come from
   environment variables set per command, so nothing in a checkout can
   change who a commit is from.
5. **Attempt configured salvage before reset.** Capture is best effort and
   size-limited (§2.4); it is not a guarantee that every dirty byte survives.
   Slot clean never uses `-x`.
6. **Deletion needs proof.** A slot is reaped only above the cap, with an
   exact identity match under a row lock, no lock, session, integration
   owner, claim file, live process or dirty path. A failed proof keeps the
   slot.
7. **One owner at a time.** The lock is one atomic conditional update that
   bumps `generation`; claim files are removed only if unchanged; jobs pin
   by generation. A stale actor can always be detected by comparing
   generations or identities.
8. **Multi-workspace acquisition is all-or-nothing in a canonical order.**
9. **Onboarding is idempotent and never destroys what it did not create.**
   Compensation removes only directories it marked, and never a GitHub
   repository.
10. **Trust an object store only after a full fsck.** Recovery moves bad
    objects aside instead of deleting them, and pack corruption needs a
    human.
11. **A repository binding is set once.** It is a compare-and-set that
    later edits cannot change.

## 5. Known gaps

These observations were checked against 52d2efe4c during the 2026-10-22 audit.
The first two are code defects to assess separately from this docs update.

- **Lock-mode spelling mismatch (locally reproduced).** The kind parser
  accepts `branch_isolated` and `directory_isolated` with underscores, but
  `WorkspaceMode` accepts hyphens. A temporary kind file parses successfully
  and then raises `ValueError` when converted to the enum; the hyphenated
  spellings are rejected by the parser. `acquire_one_unlocked` performs that
  enum conversion after its update. This audit did not test transaction
  persistence after that error.
- **`base_checkout_refusal` fails open.** A simulated database read failure
  returns `None` (launch allowed). This contradicts the isolation goal of
  keeping ordinary agents out of the operator's base checkout. The fail-open
  choice is explicit in the function's docstring; changing it needs a code
  decision.
- **Legacy clones still use `git stash`** in their cleanup ladder. Slots
  never do; a repository's worktrees share its stash stack.
- **Kind files are read only at daemon start.** An edited
  `workspace-kinds/*.md` needs a restart.
- **Schema leftovers.** `workspaces.kind_id` remains nullable;
  `directory-isolated` is a stub and `branch-isolated` a deprecated alias.
  The kind schema default `worktree` and upgrade backfill `exclusive-clone`
  serve different purposes; the parser preserves the stored value when
  `mode` is omitted.
- **Onboarding locks are per process.** Separate processes can race the same
  target. A GitHub repository created by failed onboarding is retained for
  the operator to inspect.
- **Retained integrity limits.** Pack corruption needs supervisor recovery;
  the fsck cache is per process, and no doctor check reports a retained-store
  incident.
- **Trusted low-level callers can push target branches.** `GitManager` does
  not apply worker ownership checks; the worker plugin does (§2.8).
- **The subprocess environment inherits the daemon's.** Controlled credential
  and identity overrides do not constitute a complete environment allowlist.
- **Changing GitHub credential mode needs a daemon restart.**
- **Historical code comments remain.** The `worktree_manager.py` module header
  says `adopt_existing`, `reap_slot` and `prune_branches` are not implemented;
  all three exist. That comment is not the current capability contract.

## 6. Delivery and planning record

The original 2026-10-20 inventory and task/review statuses are historical
snapshots. This audit checked shipped source and Git history rather than
re-querying sibling tasks. In particular, two features previously listed here
as pending have shipped:

- Warm workspaces and shared compiler caches (§2.12), commit `56bde6dad`
  (2026-10-21), from fast/smooth integration task `stark-willow-95.11`.
- Native storage relocation (§2.11), commit `bbcbd5a61` (2026-10-21), from
  `stark-willow-95.12`. Tooling and a fixture-validated operator runbook are
  present; this is not evidence of a live Matter Engine migration.

The inventory also names retained prerequisite stacks (`stark-willow-95.14`)
and a unified admission view (`.13`); their delivery policy belongs to
[integration-and-delivery](integration-and-delivery.md) and
[scheduling-and-claims](scheduling-and-claims.md). Consult those system specs
for current behavior.

Distributed worker nodes remain a design direction in the original inventory
(`2026-10-06-distributed-worker-nodes.md`, review `rev-eager-quest`), not a
capability established by this audit. Its proposed execution worker would
move workspace and retained-store ownership behind a separate boundary.
The related containerized/team-hosted design was recorded as withdrawn.

Completed design records used here include retained Git corruption recovery
(`2026-10-14-retained-git-corruption-confirmed-detection-defects-and-fenc.md`,
review `rev-swift-flare`), [branch-tracking deployment](../design/branch-tracking-deployment.md),
[git identity](../git-identity.md) and [GitHub access](../github-access.md).

## 7. Further reading

- [Projects and workspaces](../../concepts/projects-and-workspaces.md) and the
  [workspaces module catalog](../../reference/modules/workspaces.md#git-boundaries)
- [Project onboarding](../../guides/project-onboarding.md),
  [worker pools](../../guides/worker-pools.md),
  [retained git corruption](../../guides/retained-git-corruption.md)
- [GitHub access](../github-access.md), [git identity](../git-identity.md),
  historical [git](../git.md)
- Design records: [workspaces v2](../design/workspaces-v2.md),
  [worktree execution](../design/worktree-execution.md) and its
  [implementation plan](../implementation/worktree-execution.md),
  [hybrid workspace pools](../design/hybrid-workspace-pools.md),
  [integration repository recovery](../design/integration-repository-recovery.md),
  [retained object integrity](../design/retained-object-integrity.md),
  [branch retirement](../design/branch-retirement.md),
  [branch-tracking deployment](../design/branch-tracking-deployment.md),
  [git completion provenance](../design/git-completion-provenance.md)
