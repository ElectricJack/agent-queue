---
tags: [spec, git, identity, installer, projects]
---

# Git commit identity

**Date:** 2026-09-30 · **Task:** `fresh-cascade-88` · **Status:** implemented

Every commit Agent Queue writes or causes carries one deterministic identity,
chosen by the operator: an installation default, optionally overridden per
project. Before this, the identity depended on who launched what. Pool workers
committed as `aq <profile> <profile@agent-queue.local>`, task launches and
`aq git commit` used the daemon user's own `git config`, and integration code
hard-coded four more addresses.

## 1. Resolution

`src/git/identity.py` is the only resolver. It works on a **pair**: a name and
an email are never taken from different sources.

| Order | Source | Where it is set |
|---|---|---|
| 1 | Project override | `projects.git_identity_name` / `git_identity_email` (Project Settings, `aq project set <id> git-identity "Name <email>"`, `edit_project`) |
| 2 | Installation default | `git_identity:` in `~/.agent-queue/config.yaml` (`aq install`, `aq system config git-identity`, `set_git_identity`, Settings › Config) |
| 3 | Fallback | `Agent Queue <agent-queue@localhost>`, used only while 1 and 2 are unset |

`resolve_git_identity(config, project)` returns the identity, its source
(`project`, `installation` or `fallback`), the installation default and the
override. `configured` is false only while the fallback is in use. The fallback
is the documented, deterministic identity of an install that has not chosen
one. It is never a person, and the settings surfaces and `aq doctor` report it
as unset (§7).

Repository and global Git configuration never take part in resolution. The
identity reaches Git as `GIT_AUTHOR_*` / `GIT_COMMITTER_*`, which outrank every
`git config` level. The installer may *read* global Git config to suggest a
default (§6).

An invalid stored value (one that fails validation) resolves as unset, so Git
never receives a malformed header. Config validation reports it.

### Validation

Both fields are single-line and trimmed, without control or format
characters, without `<` or `>`, and without the punctuation Git strips from
either end (`. , : ; " ' \`). A name is at most 200 characters. An email is one
`local@domain` address with no whitespace, at most 254 characters. A dotless
domain is accepted, because the fallback uses `localhost`. AQ cannot verify
that an address is deliverable, so it does not try.

## 2. Configuration and storage

```yaml
git_identity:
  name: Jane Doe
  email: 12345+jane@users.noreply.github.com
  source: gh:jane      # informational: gh:<login>, git-config or manual
```

- Set both `name` and `email`, or neither. A half-set section fails config
  validation. Removing the section returns the install to unset.
- The section is **hot-reloadable** (`HOT_RELOADABLE_SECTIONS`). It is not in
  the portable `.aqbundle` set, because it is an operator's choice for this
  machine.
- `projects.git_identity_name` / `git_identity_email` are nullable, and
  `ck_projects_git_identity_pair` makes them set-together or NULL-together. A
  NULL pair means the project inherits.
- `sessions.git_identity_digest` records the digest of the identity injected
  into each launch (§4). Migration `a00000000053` adds all three columns
  idempotently and stamps every existing session row `legacy`.

## 3. Surfaces

| Surface | Reads | Writes |
|---|---|---|
| `get_git_identity` (`aq system get-git-identity`, `POST /api/system/get_git_identity`) | installation default, `configured`, fallback; with `project_id`, the project's effective identity and source | — |
| `set_git_identity` (`aq system set-git-identity`, `aq system config git-identity`) | — | the installation default, through the same validated, backed-up, hot-reloaded path as `update_config`; `clear: true` unsets it |
| `get_project` | `git_identity_name`, `git_identity_email` (the raw override) and `git_identity` (the effective identity with its source, the installation default and the fallback) | — |
| `edit_project` | — | `git_identity_name` / `git_identity_email`. Set both to override, or set both to `""` to reset to inheritance. A field you leave out keeps its stored value, but the result must still be a full pair. |
| Dashboard Project Settings | the effective identity with a "Project override" / "Installation default" / "Fallback — installation default not configured" label | override, edit, reset |

Identity is operator policy. Agent sessions and playbook steps
(`PrincipalKind.SESSION` / `PLAYBOOK`) get `operator_only` from
`set_git_identity` and from identity edits through `edit_project`. A worker
that could change the identity could also launder its own commits past §5.

## 4. Where the identity is applied

**Worker sessions.** `SessionSpecBuilder.build_task_spec` and `build_pool_spec`
take the project's resolved identity. They inject it after every caller-supplied
or harness-supplied variable, so it applies to every harness (`claude`,
`codex`, `gemini`), and they record its digest on the session row. The
per-profile `@agent-queue.local` address is gone. Git keeps the original author
on rebase, cherry-pick, `commit --amend` and `am`, so the author variables only
apply to new authorship.

**Daemon-side commits.** The orchestrator installs a resolver on its
`GitManager` (`set_identity_resolver`). For the commit-writing subcommands
(`commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `pull`,
`commit-tree`, `stash`, `notes`, `tag`), every call gets an identity in this
order:

1. a `commit_identity(identity)` scope (a `ContextVar`);
2. else the installation identity from the resolver.

A call that passes its own `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` keeps it. The
daemon user's own `git config` is never used. These paths open a
project-scoped `commit_identity`:

| Path | Scope |
|---|---|
| Completion pipeline (`_run_completion_pipeline`: auto-remediation, merges, rebases) | the task's project |
| Git plugin commands that can commit (`git_commit`, `commit_changes`, `git_merge`, `merge_branch`, `git_pull`, `generate_readme`) | the calling worker's project, else the named or active project |
| Pause checkpoints (`capture_checkpoint`) | the task's project |
| Plan-file cleanup before a task (`_cleanup_plan_files_before_task`) | the task's project |
| Provider-failover WIP checkpoint (`inflight.checkpoint_workspace`) | the task's project |
| Owner recovery preservation commits | the project owning the branch row |
| Development publisher (`DevelopmentIntegration.sweep`) | the swept project |

A scope never travels through the event bus. Playbook runs that an event
triggers start in `detached_commit_context()`, so a run spawned inside
project A's scope does not keep A's identity.

Deterministic plumbing commits resolve the identity explicitly with
`GitManager.resolve_commit_identity(project)`:

- **Integration candidates** (`candidates.py`): a member merge keeps the
  member's first author and uses the project identity as committer. CI-repair
  preservation uses the project identity for both.
- **Promotion** (`promotion.py`): the project identity is resolved when the
  intent is reserved and pinned in `commit_metadata`, so a retry rebuilds the
  identical commit. The source's authors are preserved.
- **Manual delivery** and **project onboarding** (the README commit): the
  project identity, or the installation identity for a project not created yet.

**One fixed exception.** Provenance *ledger* objects
(`src/integration/provenance.py`) are marker commits on AQ's own refs. They
reuse the source's tree and parent and never enter delivered history. A
published record is compared by object id, so these objects must stay
byte-identical across retries and releases. They keep the identity records were
always written with, `LEDGER_IDENTITY = Agent Queue <aq@localhost>`, and never
follow the configurable identity.

## 5. The publishing boundary

Every AQ publication of a task's work is checked inside
`GitManager.apush_validated_delivery`, the same gate as the reserved-path
check. That covers the worker's `git_push` / `push_branch` and the completion
pipeline's own pushes: the auto-push at verify, and the task-branch and
default-branch pushes of worktree integration. A worker that skips
`aq git push` and closes still meets the check. Callers pass a `PublishPolicy`:

- `allowed` committers are the identity the project resolves to now, plus the
  launch identity of **every session that worked the task**
  (`task_session_attempts` joined to `sessions.git_identity_digest`). An
  operator edit made mid-task, or a retry under a new session, does not
  strand finished work.
- A `legacy` pool launch from an earlier release adds its per-profile
  identity (`legacy_pool_identity`). A `legacy` task launch carried no
  identity AQ can name, so that task's commits are reported, not refused.

The check runs on the exact tip OID the push publishes, resolved once with
`refs/heads/<branch>`, so a same-named tag cannot shadow it. "New" commits
are those reachable from the tip and from none of these:

- the delivery base;
- this branch's last published head (`refs/remotes/origin/<branch>`);
- the caller's lease OID;
- the exact remote heads of this task's direct `blocks` prerequisites in the
  same project and repository. The daemon selects branch names from persisted
  task/dependency rows and observes them in the task's authorized repository;
  local tracking refs, task descriptions, and worker-supplied SHAs are not
  proof. A missing branch authorizes nothing; an unavailable observation
  refuses publication. A stack named only in prose needs the supervisor to
  record its prerequisite before it can use this exclusion;
- every exact source head the daemon filed this task to merge: the
  `source_head` of each `integration_source_ci` row whose `repair_task_id` is
  the task (source CI repair, "merge the exact source head … preserving it
  as an ancestor"). The head comes from the daemon's record, never from a
  ref, and only that head's history is excluded; a foreign committer on any
  other new commit is still refused.

This is the same local evidence the reserved-path gate trusts. The check never
uses every `refs/remotes/*`, because a checkout can write those. More than
2000 new commits is refused rather than sampled.

- **Committer.** It must be in `allowed`. Any other committer means the worker
  replaced AQ's identity itself (`GIT_COMMITTER_*`, `-c user.*` with the env
  removed, or a copied commit). The push is refused, the commits are named,
  and the exact fix is given:
  `GIT_COMMITTER_NAME=… GIT_COMMITTER_EMAIL=… git rebase --force-rebase
  --rebase-merges <merge-base>`, then push again with `--expected-remote-oid`
  if the branch was already published. A pipeline refusal surfaces as that
  push's `push_failed` reason.
- **Author.** A different author is **reported, never refused**:
  `identity_notes` / `identity_warning` in the worker's push reply. Upstream
  and third-party authors survive cherry-pick, rebase and `am`, and blocking
  them for differing attribution would be wrong. A worker-supplied `--author`
  is therefore visible in the reply and in the published history, but is not
  treated as fatal.
- **Root delivery** (`base_ref=None`: origin has no default branch yet). Here
  the check cannot tell new commits from inherited history, so it reports
  committers instead of refusing.
- Commits already published are never judged. Old history is never rewritten
  or blocked.

Operator and supervisor pushes (`aq git push` without a worker session,
`set_default_branch`) carry no policy.

## 6. The installer

`aq install` (step `config.git-identity`, advisory and never halting) and
`aq system config git-identity` share `src/install/git_identity.py`.

- **Suggestions**, in order:
  1. the authenticated `gh` account for the host (`github.com` by default):
     its public profile email, a verified primary address when the token can
     already read `user/emails`, and the GitHub noreply address
     `<id>+<login>@users.noreply.github.com`;
  2. the global `git config` identity, labelled as such.

  Each suggestion carries its provenance label. AQ never invents an address,
  never runs `gh auth refresh`, and never asks for a scope. A refused
  `user/emails` call is skipped silently.
- **Probes** use short timeouts and an empty stdin. A missing, offline,
  signed-out or hung `gh` falls back to manual entry and never blocks the
  install. GitHub login and Git attribution are separate: nothing here switches
  accounts or touches credentials.
- **When it asks.** The interactive wizard of a fresh install asks once, and
  only if no identity is configured. It never asks under `--upgrade`,
  `--repair`, `--yes` or `--config`, and never overwrites a stored choice.
  `aq update` never runs the installer.
- **Unattended.** `--git-name/--git-email` or
  `settings.git_identity: {name, email}` in the `--config` file set the default
  explicitly, and an explicit value does replace a stored one. With no explicit
  value, nothing is guessed or saved. The step reports that the install is
  unset and names `aq system config git-identity`.

An upgraded install that never chose an identity keeps working: it commits as
the fallback (§1) until the operator sets one.

## 7. When settings take effect

- An edit applies to **commits made by sessions launched after it**, and at
  once to daemon-side commits, which resolve per commit.
- A running **pool session** carries its launch identity in its environment.
  At its next claim, `ClaimCommandsMixin._stale_git_identity` compares the
  recorded digest with what the project resolves to now. If they differ, or
  the digest is `legacy`, the claim answers `session_exhausted` with a reason
  naming the new identity, and the session row is marked
  `desired_state=stopped`. The pool relaunches it under the new identity, so a
  new claim never inherits a stale or another project's identity. A row with
  no recorded digest is not judged at claim, but §5 still applies to its
  commits.
- A **task launch** holds one task, so its identity is fixed for that task.
  §5 accepts its launch identity.
- Projects never share a session (a pool session is bound to one project), and
  resolution reads only that project's row, so overrides cannot leak between
  projects.

## 8. Provenance and diagnostics

Model and agent provenance stay in AQ's records (the task, its session, the
profile and the claim) and in whatever trailers a harness adds itself (for
example Claude's `Co-Authored-By`). They are never encoded in a fabricated
address.

`aq doctor --check git.identity_unset` is WARN while any project commits as the
fallback and OK once a default is chosen. It lists the projects that override.
The stall check `delivery_stale` finds AQ deliveries by committer (the
project's resolved email plus the earlier fixed addresses), because an
operator's own address may now be AQ's identity.

## 9. Not done

- AQ does not re-sign, rewrite or re-attribute existing history.
- AQ does not enforce identity on operator or supervisor pushes, or on
  third-party authors.
- There is no per-profile or per-model identity. Provenance lives in AQ's
  records (§8).
- Named sessions (the supervisor, agent terminals) have no workspace and get
  no injected identity. They do not commit to project repositories.
