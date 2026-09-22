---
name: aq-workspaces-and-git
description: Workspace and git operations for aq worker sessions — the isolated worktree you were assigned, the branch it lives on, and how to commit / push / open PRs from the CLI. Use when you need to check where you're working, inspect the branch state, commit your changes, push, or open a pull request. Also covers workspace release / doctor / reap.
allowed-tools:
  - Bash
---

# aq workspaces + git

## Where am I working?

Every worker task acquires a workspace at start. Two places tell you
where you are:

```bash
pwd                              # the working directory the daemon put you in
echo "$AQ_WORK_DIR"              # the same path, from the session environment
aq prime                         # restates work_dir + branch for the held task
```

The branch is on the task row; the workspace path is not, so read the path
from the environment above rather than from the task:

```bash
aq --json task show <task_id> | jq -r '.data.branch_name'
```

An operator (not a worker token) can see every workspace and its lock holder:

```bash
aq project list-workspaces --project-id <pid>   # every workspace + who holds each lock
```

## Local Git work

`git` is on `PATH` in every worker session. Use it for local status, history,
edits and commits. Use `aq git` for network publication: the daemon supplies
the project's GitHub credentials, including an App credential when configured.
The worker shell does not receive a GitHub App credential.

```bash
git status
git log --oneline -10
git diff <base>..HEAD --stat
git branch --show-current
```

## Committing your work

Every task closes with commits on its branch. The typical pattern:

```bash
git add -A                        # or targeted paths
git commit -m "$(cat <<'EOF'
feat(scope): concise subject

Longer body explaining the *why*, not the *what*.

Co-Authored-By: <your-agent-attribution>
EOF
)"
aq git push                       # first and subsequent task-branch pushes
```

Commit rules:
- **Never `--no-verify`** — pre-commit hooks catch regressions before
  anyone else has to.
- **Never amend a pushed commit** — always create a new commit.
- **One commit per logical change** where practical. If you did five
  small independent things, five commits is better than one giant one.

For a review workflow that asks you to squash a branch already pushed,
record the full OID of the successful earlier push (`git rev-parse HEAD` at
that push). After the local squash, run:

```bash
aq git push --expected-remote-oid <previously-pushed-oid>
```

This is an exact lease on your task branch. If the remote moved, stop and
report the conflict. An all-zero 40-digit OID creates a branch only if it is
still absent. Do not force push reviewed or delivered history.

## Opening a PR

For tasks that carry a `pr_url` or a `--needs-pr` flag:

```bash
aq git create-pr --title "..." --body "..."
```

Integration and anyone auditing the task read the branch from
`origin/<branch>`, so push it before you call `aq task close`.

## Stacked branches

Branch from the default branch (`main`). Stack on a prerequisite task's
branch **only** when the work genuinely cannot build or run without it:
declare the dependency on that task and say so in your close summary.

Delivery to the default branch belongs to the project's configured
integration owner, not to the last task in a stack (software-factory policy,
`docs/concepts/factory-policy.md`). Follow the delivery section of your prime: open a PR
only when your task or project asks for one, and never merge it yourself.

A PR merged into a feature branch has shipped nothing to `main`. The tasks
close COMPLETED and dependents are released, but `main` does not have the
code — this is exactly how `feature/playbook-v2-pkg4-core` swallowed three
merged PRs. `aq doctor --check pools.stranded_feature_branches` lists
branches in that state.

## Never leave commits only in your worktree

Your slot is reset for the next task as soon as you let go of it. Commits that
no remote branch carries are unreachable from that point on — not by a retry,
not by a reviewer, not by you.

```bash
aq git push                       # before you close, every time
git rev-parse HEAD                # local commit to compare with the push result
```

A close with `--outcome fail` and unpushed commits is not silently accepted:
the daemon pushes them to `aq/<task-id>` (or `aq/<task-id>-wip`) and records
the branch in your completion summary. If the push fails, the close is refused
and the task stays yours until you fix the problem and retry `aq git push`.

If `aq git push` or `aq git create-pr` reports a missing profile grant, tell
the supervisor. The operator can run `aq doctor --check profiles.system_drift`
and add only the missing worker-template grants with
`aq agent profile-reseed --profile-id worker-<harness> --grants-only`.
This preserves customized vault profiles; a full reseed replaces their text.

## Reviewers and read-only workspaces

If your profile has `read_only: true` (reviewer / final-reviewer), you
hold an ordinary slot worktree — it is yours for the task and nobody
else is writing to it. `read_only` is a statement about *intent*, not
about isolation: don't `git commit` or `git push`, because reviewing is
not the job that produces commits. Stick to `git log --oneline`,
`git show <sha>`, `git diff <base>..<branch>`.

Your work_dir is never the project's base checkout — that clone exists
for `fetch` and `git worktree` bookkeeping and is often a human's own
working tree, so the daemon refuses to launch a session in it.

## Workspace admin commands

These are operator reads: a plain worker or reviewer token gets
`out of scope: <command>` back, and reaping is elevated on top of that.

```bash
aq project list-workspaces --project-id <pid>     # all workspaces + lock holders
aq project workspace-doctor --project-id <pid>    # check locks + git health
aq project workspace-reap --project-id <pid>      # (elevated) cull dead worktrees
```

There is no per-workspace show command — `list-workspaces` is the detail
view, filtered by project.

## Release + attach

You do not normally release the workspace — task close does it. If you
absolutely need to (e.g. hand off mid-run), the supervisor should call
`aq project release-workspace --workspace-id <id>` on your behalf.
