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
aq project list-workspaces --project <pid>   # every workspace + who holds each lock
```

For the specific workspace bound to your task:

```bash
aq task get <task_id> --json | jq '.workspace_path, .branch_name'
```

## Git via plain CLI

`git` is on `PATH` in every worker session. Prefer plain `git` over any
`aq git`/MCP wrapper — it's the same underlying operation with fewer
layers.

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
git push -u origin HEAD           # first push on a new branch
git push                          # subsequent
```

Rules of thumb the reviewer stage enforces:
- **Never `--no-verify`** — pre-commit hooks catch regressions the
  reviewer would flag anyway.
- **Never amend a pushed commit** — always create a new commit.
- **One commit per logical change** where practical. If you did five
  small independent things, five commits is better than one giant one.

## Opening a PR

For tasks that carry a `pr_url` or a `--needs-pr` flag:

```bash
gh pr create --title "..." --body "$(cat <<'EOF'
## Summary
...

## Test plan
...
EOF
)"
```

The reviewer stage reads the diff from origin/<branch>, so the PR must
be pushed and reachable before you call `aq task close`.

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

Non-elevated sessions can inspect; only supervisor / operator elevates.

```bash
aq project list-workspaces --project <pid>
aq workspace show <workspace_id>          # single workspace detail
aq workspace doctor --project <pid>       # check locks + git health
aq workspace reap --project <pid>         # (elevated) cull dead worktrees
```

## Release + attach

You do not normally release the workspace — task close does it. If you
absolutely need to (e.g. hand off mid-run), the supervisor should call
`aq workspace release --workspace-id <id>` on your behalf.
