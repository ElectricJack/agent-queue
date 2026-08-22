# Dashboard v2 Phase 2 — Review Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the policy-derived review layer: per-task + per-branch review stages spawned by the default pipeline playbook, reviewer/final-reviewer agent-type profiles, close-with-summary enforcement, PR merge authority, and a clean rework loop via `reopen_with_feedback` — all reactions live in vault markdown, the framework only wires deterministic actions on `task.completed`.

**Architecture:**
- **Pipeline rules only.** Two new rules land in the shipped default system pipeline playbook (`vault/playbooks/default-pipeline.md`, seeded from `src/prompts/default_playbooks/default-pipeline.md`): a `scope: task` rule spawns a review task per completed task with a branch, gates all downstream dependents with `task` gates; a `scope: branch` rule uses `ensure_task(dedup_key = "branch-review:"+branch)` to keep one final-review-per-branch and wires per-task reviews as `blocks` edges into it. Downstream work also carries `pr-merged` gates (existing type, auto-resolved by `_sweep_resolve_pr_ci_gates`).
- **Reviewer / final-reviewer are vault agent-type profiles** — normal tmux agents. Reviewer reads the diff/PR and either closes-with-summary (approval) or calls `reopen_with_feedback` on the reviewed task (rejection). Final reviewer has one extra tool: `pr_merge` (new command that shells out to `gh pr merge`).
- **Close-with-summary enforcement** lands in `session_commands.py::_cmd_task_close` — for tasks whose profile has `needs_workspace: true`, `summary` becomes required (and the commit hash is captured from `branch_name` via git rev-parse). Pure error-return, agent-legible.
- **Rework loop** already fires a fresh `task.completed` event on re-completion; each pipeline reaction is `(playbook_id, event_id)`-unique, so a reopened task gets a fresh review task on next completion. Stale prior review tasks that were still open at reopen time are auto-cancelled by a new small helper triggered on `reopen_with_feedback`.

**Tech Stack:** Python 3.12 (async), SQLAlchemy Core, pytest-asyncio, existing pipeline playbook runner (Phase 1), existing `gates` / `task_gates` / `task_dependencies` schema, `gh` CLI for PR merge.

## Global Constraints

- No new `TaskStatus` enum values. Review is graph structure (gates + edges), not state. (spec §3, §7)
- No LLM in the pipeline runner path. Actions are deterministic `CommandHandler.execute` calls. (spec §4.2)
- Pipeline runs are idempotent per `(playbook_id, event_id)`; duplicate `task.completed` never double-fires. (spec §4.4)
- Review edges are `discovered-from`, **never** `parent-child` — parent-child release semantics fire too early. (spec §7)
- `ensure_task(project_id, dedup_key, ...)` is find-or-create; every review-task creation flows through it. (spec §4.4, Phase 1 contract)
- Worker profiles MUST NOT ship `pr_merge` in `allowed_tools`; only the final-reviewer profile does. (spec §7)
- All new commands return `{"success": bool, ...}` dicts and go through `CommandHandler`. (CLAUDE.md)
- Reviewer profiles have `needs_workspace: true` with `read_only: true` (they read the diff, they do not commit); final-reviewer has `needs_workspace: true` writable (needs a checkout to run `gh pr merge`).
- Async-first: any git shellouts go through `GitManager` `a`-prefixed methods, never `subprocess.run()` in production paths.
- After ANY change to `src/database/tables.py`, run `alembic revision --autogenerate -m "..."` and review the generated file. (This plan does **not** modify `tables.py`.)

---

## File Structure

**New files:**
- `vault/agent-types/reviewer/profile.md` — reviewer agent-type profile (system-scope, seeded default)
- `vault/agent-types/final-reviewer/profile.md` — final-reviewer agent-type profile (system-scope)
- `src/prompts/default_profiles/reviewer.md` — packaged source for the reviewer profile (installed to vault on first run, like other defaults)
- `src/prompts/default_profiles/final-reviewer.md` — packaged source for final-reviewer profile
- `tests/test_review_pipeline_rules.py` — unit tests for the two default rules (parse + action execution)
- `tests/test_pr_merge_command.py` — unit tests for the new `pr_merge` command
- `tests/test_task_close_summary_enforcement.py` — unit tests for close-with-summary requirement
- `tests/test_review_reopen_cascade.py` — integration test for the rework loop (reopen cancels stale review, fresh completion spawns fresh review)
- `tests/test_review_pipeline_e2e.py` — integration test of the full chain against a fake completed task

**Modified files:**
- `src/prompts/default_playbooks/default-pipeline.md` — add two `on: task.completed` rules (task-scope review, branch-scope final review). If the file does not exist yet at execution time (Phase 1 landed it), this task creates it with only the review rules; Phase 1's rules must be preserved by the executor.
- `src/commands/session_commands.py` — enforce `summary` requirement in `_cmd_task_close` for `needs_workspace: true` profiles; capture commit hash from `branch_name`.
- `src/commands/task_commands.py` — extend `_cmd_reopen_with_feedback` to cancel stale open review tasks (`discovered-from` edges pointing at the reopened task) so a reopened task does not carry ghost reviews.
- `src/git/manager.py` — add `amerge_pr(checkout_path, pr_url, method="squash")` (thin `gh pr merge` wrapper).
- `src/commands/git_commands.py` (or wherever git commands live — grep for `_cmd_create_pr` and place alongside) — add `_cmd_pr_merge` command routing to `GitManager.amerge_pr`.
- `src/mcp_registration.py` — expose the new `pr_merge` command over MCP (auto-registered via existing scanner if the naming pattern is followed; verify).

---

### Task 1: Add `pr_merge` GitManager wrapper + command

**Files:**
- Modify: `src/git/manager.py` (add `amerge_pr` method after `acheck_pr_merged` at ~line 1954)
- Modify: `src/commands/git_commands.py` (add `_cmd_pr_merge` — if file does not exist, grep the codebase for the mixin that owns `_cmd_create_pr` and place it there; the plan step below shows both possibilities)
- Test: `tests/test_pr_merge_command.py`

**Interfaces:**
- Consumes: `GitManager` (existing), `CommandHandler` (existing), `Project.workspace_path` (existing model field).
- Produces:
  - `GitManager.amerge_pr(checkout_path: str, pr_url: str, method: str = "squash") -> dict` returning `{"success": bool, "sha": str | None, "error": str | None}`.
  - `CommandHandler._cmd_pr_merge(args: dict) -> dict` — args: `{"project_id": str, "pr_url": str, "method": "squash" | "merge" | "rebase" (default "squash")}`. Returns `{"success": bool, "pr_url": str, "sha": str | None, "error": str | None}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_merge_command.py
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_pr_merge_command_shells_gh_and_returns_success(monkeypatch):
    from src.git.manager import GitManager
    gm = GitManager(base_dir="/tmp")

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[:3] == ["gh", "pr", "merge"]
        assert "--squash" in cmd
        assert "https://github.com/org/repo/pull/42" in cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "Merged\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pr_merge_command_reports_gh_failure(monkeypatch):
    from src.git.manager import GitManager
    gm = GitManager(base_dir="/tmp")

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "not mergeable: conflicts"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is False
    assert "conflicts" in result["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pr_merge_command.py -v`
Expected: FAIL — `AttributeError: 'GitManager' object has no attribute 'amerge_pr'`.

- [ ] **Step 3: Implement `amerge_pr` in `src/git/manager.py`**

Add after the `acheck_pr_merged` method (around line 1990). Follow the existing `acreate_pr` pattern for shellout + timeout handling.

```python
    async def amerge_pr(
        self,
        checkout_path: str,
        pr_url: str,
        method: str = "squash",
    ) -> dict:
        """Merge a PR via ``gh pr merge``.

        Parameters
        ----------
        checkout_path:
            Any valid checkout of the repo (gh reads the remote from here).
        pr_url:
            Full PR URL, e.g. ``https://github.com/org/repo/pull/42``.
        method:
            One of ``"squash"``, ``"merge"``, ``"rebase"``.  Defaults to
            ``"squash"`` — matches the project convention documented in
            the shipped final-reviewer profile.

        Returns
        -------
        dict
            ``{"success": bool, "sha": str | None, "error": str | None}``.
            ``sha`` is best-effort — gh only prints it in some flows;
            callers who need the merged sha should query the branch head
            after this returns.
        """
        if method not in ("squash", "merge", "rebase"):
            return {"success": False, "sha": None, "error": f"invalid method: {method}"}
        flag = f"--{method}"
        try:
            result = await self._arun_subprocess(
                ["gh", "pr", "merge", pr_url, flag, "--delete-branch"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "sha": None, "error": "gh pr merge timed out"}
        if result.returncode != 0:
            return {
                "success": False,
                "sha": None,
                "error": (result.stderr or result.stdout or "gh pr merge failed").strip(),
            }
        # gh prints "Merged pull request #N (<sha>)" in some flows; best-effort parse.
        sha: str | None = None
        for tok in (result.stdout or "").split():
            if len(tok) == 40 and all(c in "0123456789abcdef" for c in tok):
                sha = tok
                break
        return {"success": True, "sha": sha, "error": None}
```

- [ ] **Step 4: Run test to verify GitManager tests pass**

Run: `pytest tests/test_pr_merge_command.py::test_pr_merge_command_shells_gh_and_returns_success tests/test_pr_merge_command.py::test_pr_merge_command_reports_gh_failure -v`
Expected: PASS.

- [ ] **Step 5: Add the CommandHandler entry — write failing test**

Append to `tests/test_pr_merge_command.py`:

```python
@pytest.mark.asyncio
async def test_cmd_pr_merge_routes_through_git_manager(monkeypatch, command_handler_factory):
    handler = await command_handler_factory()
    # Seed a project with a workspace path.
    await handler.db.create_project(id="p1", name="P1", workspace_path="/tmp/p1")
    calls = {}

    async def fake_amerge(checkout_path, pr_url, method="squash"):
        calls["args"] = (checkout_path, pr_url, method)
        return {"success": True, "sha": "abc123", "error": None}

    monkeypatch.setattr(handler.git, "amerge_pr", fake_amerge)
    result = await handler.execute("pr_merge", {
        "project_id": "p1",
        "pr_url": "https://github.com/o/r/pull/1",
        "method": "squash",
    })
    assert result["success"] is True
    assert result["sha"] == "abc123"
    assert calls["args"] == ("/tmp/p1", "https://github.com/o/r/pull/1", "squash")


@pytest.mark.asyncio
async def test_cmd_pr_merge_rejects_unknown_project(command_handler_factory):
    handler = await command_handler_factory()
    result = await handler.execute("pr_merge", {
        "project_id": "nope",
        "pr_url": "https://github.com/o/r/pull/1",
    })
    assert result["success"] is False
    assert "project" in result["error"].lower()
```

`command_handler_factory` is the existing conftest fixture used by tests in `tests/test_command_surface.py`. If a different fixture name is in use, adapt to it — grep `tests/conftest.py` for the actual name.

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_pr_merge_command.py -v`
Expected: FAIL — `pr_merge` command not registered.

- [ ] **Step 7: Implement `_cmd_pr_merge`**

Grep for the file that owns `_cmd_create_pr` (`grep -rn "_cmd_create_pr" src/commands/`). Add the new method in the same mixin class so MCP auto-registration picks it up.

```python
    async def _cmd_pr_merge(self, args: dict) -> dict:
        """Merge a PR.  Backs ``aq pr merge`` and the final-reviewer's tool.

        Only allowed for profiles that whitelist ``pr_merge`` in
        ``allowed_tools`` — the profile system enforces the toolset per
        agent, so worker profiles cannot invoke this even if they discover
        the command name.
        """
        project_id = args.get("project_id")
        pr_url = args.get("pr_url")
        method = str(args.get("method") or "squash")
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        if not pr_url:
            return {"success": False, "error": "pr_url is required"}
        project = await self.db.get_project(project_id)
        if project is None:
            return {"success": False, "error": f"unknown project: {project_id}"}
        checkout_path = project.workspace_path
        if not checkout_path:
            return {"success": False, "error": f"project {project_id} has no workspace_path"}
        result = await self.git.amerge_pr(checkout_path, pr_url, method=method)
        return {
            "success": result["success"],
            "pr_url": pr_url,
            "sha": result.get("sha"),
            "error": result.get("error"),
        }
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_pr_merge_command.py -v`
Expected: PASS (all four cases).

- [ ] **Step 9: Commit**

```bash
git add src/git/manager.py src/commands/git_commands.py tests/test_pr_merge_command.py
git commit -m "feat(git): add pr_merge command for final-reviewer merge authority"
```

---

### Task 2: Enforce close-with-summary for workspace-needing profiles

**Files:**
- Modify: `src/commands/session_commands.py` — extend `_cmd_task_close` at ~line 372 to require `summary` when the task's profile has `needs_workspace: true`; capture commit hash from `branch_name` via `GitManager.arev_parse` (or shellout equivalent — see step 3).
- Test: `tests/test_task_close_summary_enforcement.py`

**Interfaces:**
- Consumes: existing `_cmd_task_close(args)` signature — args already accept `summary`, `commit`, `branch`, `notes`. Task model has `branch_name` (nullable). Profile has `config: dict` (parsed from `## Config` fenced JSON); we read `config.get("needs_workspace") is True`.
- Produces: no new public API — only stricter validation and one new task_metadata key: `work_commit_auto` (the auto-derived commit sha) when the agent did not pass `commit` and a branch exists.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_task_close_summary_enforcement.py
import pytest

@pytest.mark.asyncio
async def test_close_rejects_missing_summary_for_workspace_profile(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    # Seed a profile with needs_workspace: true
    await h.db.upsert_agent_profile(id="worker", name="Worker", config={"needs_workspace": True})
    task = await h.execute("create_task", {"project_id": "p", "title": "t", "profile_id": "worker"})
    tid = task["task_id"]
    await h.db.transition_task(tid, "IN_PROGRESS", context="test")
    result = await h.execute("task_close", {"task_id": tid, "outcome": "success"})
    assert result["success"] is False
    assert "summary" in result["error"].lower()


@pytest.mark.asyncio
async def test_close_allows_missing_summary_for_supervisor_profile(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    await h.db.upsert_agent_profile(id="chat", name="Chat", config={"needs_workspace": False})
    task = await h.execute("create_task", {"project_id": "p", "title": "t", "profile_id": "chat"})
    tid = task["task_id"]
    await h.db.transition_task(tid, "IN_PROGRESS", context="test")
    result = await h.execute("task_close", {"task_id": tid, "outcome": "success"})
    assert result["success"] is True


@pytest.mark.asyncio
async def test_close_captures_commit_from_branch(command_handler_factory, monkeypatch):
    h = await command_handler_factory()
    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    await h.db.upsert_agent_profile(id="worker", name="Worker", config={"needs_workspace": True})
    task = await h.execute("create_task", {"project_id": "p", "title": "t", "profile_id": "worker"})
    tid = task["task_id"]
    await h.db.update_task(tid, branch_name="feature/x")
    await h.db.transition_task(tid, "IN_PROGRESS", context="test")

    async def fake_arev(checkout, ref):
        assert ref == "feature/x"
        return "deadbeef" * 5  # 40 chars
    monkeypatch.setattr(h.git, "arev_parse", fake_arev, raising=False)

    result = await h.execute("task_close", {
        "task_id": tid, "outcome": "success", "summary": "did the thing",
    })
    assert result["success"] is True
    meta = await h.db.get_task_meta(tid, "work_commit_auto")
    assert meta == "deadbeef" * 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_task_close_summary_enforcement.py -v`
Expected: FAIL — either (a) `summary` accepted despite `needs_workspace`, or (b) `work_commit_auto` never written.

- [ ] **Step 3: Add `arev_parse` to GitManager if missing**

Grep `src/git/manager.py` for `def arev_parse`. If absent, add:

```python
    async def arev_parse(self, checkout_path: str, ref: str) -> str | None:
        """Return the SHA for ``ref`` in ``checkout_path``, or None.

        Best-effort: returns None on any failure (missing checkout,
        unknown ref, gh/git error).  Callers must not raise on None.
        """
        try:
            result = await self._arun_subprocess(
                ["git", "rev-parse", "--verify", ref],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        sha = (result.stdout or "").strip()
        return sha if len(sha) == 40 else None
```

- [ ] **Step 4: Extend `_cmd_task_close`**

In `src/commands/session_commands.py`, after the `task = await self.db.get_task(str(task_id))` block and before writing outcome metadata, insert the enforcement + capture:

```python
        # --- close-with-summary enforcement (Dv2 Phase 2 §7) --------------
        # Tasks executed by workspace-needing profiles must carry a
        # summary at close time.  This is what feeds the reviewer, the
        # dashboard completion card, and the task-summary note in the
        # vault.  Supervisor / chat-only profiles skip the requirement
        # because they never touch a repo.
        summary = str(args.get("summary") or "").strip()
        profile = None
        if task.profile_id:
            profile = await self.db.get_agent_profile(task.profile_id)
        needs_ws = bool((profile.config or {}).get("needs_workspace")) if profile else False
        if needs_ws and not summary:
            return {
                "success": False,
                "error": (
                    "summary is required for tasks whose profile has "
                    "needs_workspace: true (Dv2 Phase 2 §7 close contract)"
                ),
            }
        if summary:
            await self.db.set_task_meta(task_id, "summary", summary)

        # Capture commit hash from branch when the agent did not supply
        # ``commit`` explicitly.  Best-effort — a missing branch, a
        # missing checkout, or a git error all leave ``work_commit_auto``
        # unset (rather than failing the close).
        if needs_ws and not args.get("commit") and task.branch_name:
            project = await self.db.get_project(task.project_id)
            checkout = getattr(project, "workspace_path", None) if project else None
            if checkout:
                sha = await self.git.arev_parse(checkout, task.branch_name)
                if sha:
                    await self.db.set_task_meta(task_id, "work_commit_auto", sha)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_task_close_summary_enforcement.py -v`
Expected: PASS.

- [ ] **Step 6: Regression check**

Run: `pytest tests/test_command_surface.py tests/test_cli.py -v`
Expected: PASS (no existing test relied on close-without-summary for workspace profiles; if any breaks, it is the profile fixture missing `needs_workspace: true` which is a legitimate policy change — update the fixture to supply a summary).

- [ ] **Step 7: Commit**

```bash
git add src/commands/session_commands.py src/git/manager.py tests/test_task_close_summary_enforcement.py
git commit -m "feat(session): enforce summary + capture commit on close for repo profiles"
```

---

### Task 3: Ship reviewer and final-reviewer agent-type profiles

**Files:**
- Create: `src/prompts/default_profiles/reviewer.md`
- Create: `src/prompts/default_profiles/final-reviewer.md`
- Modify: `src/profiles/sync.py` OR the default-profile installer — grep for how existing defaults land in the vault; if a hard-coded list of default profile filenames exists, add the two new files to it.
- Test: `tests/test_default_profiles.py` — add cases asserting the two profiles are installed and parse cleanly.

**Interfaces:**
- Consumes: existing profile-installer path (grep for `default_profiles`), the `ProfileParser` (`src/profiles/parser.py`), the pipeline runner (which references these profiles by `id`).
- Produces: two vault agent-type profiles reachable at `vault/agent-types/reviewer/profile.md` and `vault/agent-types/final-reviewer/profile.md` after first startup. IDs `reviewer` and `final-reviewer` — the pipeline rule in Task 4 references these IDs verbatim.

- [ ] **Step 1: Create `src/prompts/default_profiles/reviewer.md` with the exact content below**

```markdown
---
id: reviewer
name: Reviewer
tags: [system, review, dv2-phase2]
---

## Config

```json
{
  "runtime": "claude_sdk",
  "needs_workspace": true,
  "read_only": true,
  "default_class": "focused",
  "description": "Reads the diff/PR of a completed task and either approves (closes its own review task with a summary) or rejects (calls reopen_with_feedback on the reviewed task)."
}
```

## Tools

```json
{
  "allowed": [
    "list_tasks",
    "get_task",
    "get_task_meta",
    "read_file",
    "git_log",
    "git_diff",
    "git_show",
    "gh_pr_view",
    "gh_pr_diff",
    "reopen_with_feedback",
    "task_close",
    "task_heartbeat"
  ]
}
```

## MCP Servers

```json
[]
```

## Role

You are a code reviewer. A worker agent has just completed a task on a
feature branch. Your job is to read the diff, cross-check it against the
reviewed task's title, description, and summary, and produce a verdict.

**Approval path (the code is fine):**
1. Call `task_close` on your own review task with `outcome=success` and a
   short `summary` explaining what you checked and why it is fine.

**Rejection path (the code needs rework):**
1. Call `reopen_with_feedback` on the *reviewed* task (the one whose id
   is in your task description under "Reviewing task:"). Pass
   `feedback` = a specific, actionable list of what needs to change.
2. Then call `task_close` on your own review task with `outcome=success`
   and a `summary` that says "rejected — reopened <task_id> with
   feedback".

You do not merge PRs. You do not push commits. If the reviewed task's
branch is not yet pushed or the PR is missing, reject with feedback
asking the worker to open a PR first.

## Rules

- Never edit code. Your workspace is read-only.
- Never merge. If merge authority is needed, the final-reviewer stage
  runs after all per-task reviewers approve.
- Every verdict is either `task_close(success)` OR
  `reopen_with_feedback` + `task_close(success)`. Never `task_close`
  with `outcome=failure` — a failed review is a rejection, not a failed
  task.
```

- [ ] **Step 2: Create `src/prompts/default_profiles/final-reviewer.md` with the exact content below**

```markdown
---
id: final-reviewer
name: Final Reviewer
tags: [system, review, merge-authority, dv2-phase2]
---

## Config

```json
{
  "runtime": "claude_sdk",
  "needs_workspace": true,
  "read_only": false,
  "default_class": "focused",
  "description": "Runs once per branch after all per-task reviews complete. Reads the aggregate PR, verifies CI is green, and merges the PR (this is the only profile with merge authority)."
}
```

## Tools

```json
{
  "allowed": [
    "list_tasks",
    "get_task",
    "get_task_meta",
    "read_file",
    "git_log",
    "git_diff",
    "git_show",
    "gh_pr_view",
    "gh_pr_diff",
    "gh_run_view",
    "pr_merge",
    "reopen_with_feedback",
    "task_close",
    "task_heartbeat"
  ]
}
```

## MCP Servers

```json
[]
```

## Role

You are the final reviewer for a branch. Every per-task review that fed
into this branch has already approved. Your job:

1. Read the aggregate PR (its URL is on your task under
   `pr_url` / `task_meta:pr_url` for the branch). Use `gh_pr_view` and
   `gh_pr_diff` to confirm the diff still matches what the per-task
   reviewers approved (no surprise force-pushes).
2. Check CI: `gh_run_view` on the latest run for the branch. If CI is
   not green, either wait for it (call `task_heartbeat` and re-check
   later) or reject the branch: reopen every completed task on this
   branch (`reopen_with_feedback` on each) with a note about the CI
   failure, then close your own task with `outcome=success` and a
   summary that says "rejected — CI red on <run_url>".
3. If everything checks out, call `pr_merge` with `method=squash`, then
   close your own task with `outcome=success` and a summary that
   includes the merge sha and the PR URL.

## Rules

- You are the ONLY profile with `pr_merge` in its toolset. Guard that
  authority carefully — a bad merge is user-visible and expensive to
  revert.
- Never merge without checking CI. Never merge on a diff that does not
  match what per-task reviewers approved.
- Never edit code yourself. If the branch needs fixes, reject via
  `reopen_with_feedback` on the worker tasks.
```

- [ ] **Step 3: Wire the new profiles into the default installer**

Grep: `grep -rn "default_profiles" src/profiles/ src/orchestrator/ src/main.py | head`. Locate the file that enumerates default profile filenames (likely `src/profiles/sync.py` or `src/main.py` startup). If a manifest list exists, add `"reviewer.md"` and `"final-reviewer.md"`. If the installer picks up every `*.md` in `src/prompts/default_profiles/` automatically, no code change is needed — verify with a quick test run in step 5.

- [ ] **Step 4: Add default-profile parse tests**

Append to `tests/test_default_profiles.py`:

```python
def test_reviewer_profile_parses_and_lacks_merge_authority():
    from pathlib import Path
    from src.profiles.parser import ProfileParser
    src = Path("src/prompts/default_profiles/reviewer.md").read_text()
    parsed = ProfileParser().parse(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "reviewer"
    tools = parsed.tools.get("allowed", [])
    assert "pr_merge" not in tools, "reviewer must not have merge authority"
    assert "reopen_with_feedback" in tools
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is True


def test_final_reviewer_profile_has_merge_authority():
    from pathlib import Path
    from src.profiles.parser import ProfileParser
    src = Path("src/prompts/default_profiles/final-reviewer.md").read_text()
    parsed = ProfileParser().parse(src)
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "final-reviewer"
    tools = parsed.tools.get("allowed", [])
    assert "pr_merge" in tools, "final-reviewer must have merge authority"
    assert parsed.config.get("needs_workspace") is True
    assert parsed.config.get("read_only") is False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_default_profiles.py -v -k "reviewer or final_reviewer"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/prompts/default_profiles/reviewer.md src/prompts/default_profiles/final-reviewer.md tests/test_default_profiles.py
# also stage any installer manifest change from step 3
git commit -m "feat(profiles): ship reviewer + final-reviewer agent-type profiles"
```

---

### Task 4: Add per-task review rule to the default pipeline playbook

**Files:**
- Modify: `src/prompts/default_playbooks/default-pipeline.md` — extend the shipped default system pipeline playbook with a `scope: task` rule on `task.completed`. If the file does not exist yet (Phase 1 hasn't shipped locally), create it with just the review rules; the executor must preserve any Phase 1 rules already present.
- Test: `tests/test_review_pipeline_rules.py`

**Interfaces:**
- Consumes: pipeline runner (Phase 1), the `action` node type, the whitelisted commands `create_task`, `add_dependency`, `get_downstream_tasks`, `gate_create`. Reads `event.task_id`, `event.project_id`, `task_meta.branch_name` (via `get_task`).
- Produces: on every `task.completed` where the completed task has a `branch_name`, one review task is created (profile `reviewer`, `discovered-from` edge to the reviewed task) and a `task` gate is attached to every downstream dependent of the reviewed task. Pipeline-run idempotency (`(playbook_id, event_id)`) ensures duplicate `task.completed` events do not create duplicate review tasks.

- [ ] **Step 1: Write failing test (rule parses + fires the right actions)**

```python
# tests/test_review_pipeline_rules.py
import pytest
from pathlib import Path

@pytest.mark.asyncio
async def test_per_task_review_rule_parses():
    from src.playbooks.compiler import compile_playbook
    src = Path("src/prompts/default_playbooks/default-pipeline.md").read_text()
    compiled = compile_playbook(src)
    assert compiled.errors == [], compiled.errors
    # The compiled JSON must contain a rule keyed by ``role: default-pipeline``
    # with a scope:task review action node.
    rules = compiled.rules or compiled.nodes
    assert any(
        r.get("scope") == "task" and any(
            n.get("command") == "create_task" and "reviewer" in str(n.get("args", {}))
            for n in r.get("nodes", [r])
        )
        for r in (rules if isinstance(rules, list) else rules.values())
    ), "no scope:task rule creating a reviewer task found"


@pytest.mark.asyncio
async def test_per_task_review_fires_on_completion_with_branch(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = await pipeline_engine_factory(handler=h)
    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    await h.db.upsert_agent_profile(id="worker", name="Worker", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="Reviewer", config={"needs_workspace": True, "read_only": True})

    # Reviewed task + one downstream dependent.
    t1 = (await h.execute("create_task", {"project_id": "p", "title": "T1", "profile_id": "worker"}))["task_id"]
    t2 = (await h.execute("create_task", {"project_id": "p", "title": "T2", "profile_id": "worker"}))["task_id"]
    await h.execute("add_dependency", {"task_id": t2, "depends_on_task_id": t1, "dep_type": "blocks"})
    await h.db.update_task(t1, branch_name="feature/t1")

    # Fire the pipeline reaction for task.completed on t1.
    await engine.dispatch("task.completed", {"task_id": t1, "project_id": "p", "title": "T1"})

    # A review task exists, discovered-from t1, profile=reviewer.
    tasks = await h.db.list_tasks(project_id="p")
    reviews = [t for t in tasks if t.profile_id == "reviewer"]
    assert len(reviews) == 1
    edges = await h.db.get_dependencies_for_task(reviews[0].id)
    assert any(e.dep_type == "discovered-from" and e.depends_on_task_id == t1 for e in edges)

    # A ``task`` gate is attached to t2 awaiting the review's completion.
    gates = await h.db.get_gates_for_task(t2)
    assert any(g["gate_type"] == "task" and g["await_id"] == reviews[0].id for g in gates)


@pytest.mark.asyncio
async def test_per_task_review_skips_when_no_branch(command_handler_factory, pipeline_engine_factory):
    h = await command_handler_factory()
    engine = await pipeline_engine_factory(handler=h)
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="chat", name="Chat", config={"needs_workspace": False})
    t = (await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "chat"}))["task_id"]
    await engine.dispatch("task.completed", {"task_id": t, "project_id": "p", "title": "T"})
    tasks = await h.db.list_tasks(project_id="p")
    assert all(x.profile_id != "reviewer" for x in tasks), "review must not spawn for branchless tasks"


@pytest.mark.asyncio
async def test_per_task_review_is_idempotent(command_handler_factory, pipeline_engine_factory):
    h = await command_handler_factory()
    engine = await pipeline_engine_factory(handler=h)
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="Worker", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="Reviewer", config={"needs_workspace": True, "read_only": True})
    t = (await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}))["task_id"]
    await h.db.update_task(t, branch_name="feature/t")
    payload = {"task_id": t, "project_id": "p", "title": "T"}
    # Dispatching the same event_id twice must not create two reviews.
    await engine.dispatch("task.completed", payload, event_id="evt-1")
    await engine.dispatch("task.completed", payload, event_id="evt-1")
    tasks = await h.db.list_tasks(project_id="p")
    reviews = [x for x in tasks if x.profile_id == "reviewer"]
    assert len(reviews) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_pipeline_rules.py -v`
Expected: FAIL — the default pipeline playbook has no per-task review rule yet.

- [ ] **Step 3: Add the per-task review rule to `src/prompts/default_playbooks/default-pipeline.md`**

If the file exists (Phase 1 landed), read it first and preserve every existing rule; append this rule inside its top-level `rules:` list. If the file does not exist, create it with the header plus this rule as the sole entry.

```markdown
---
kind: pipeline
role: default-pipeline
id: default-pipeline
name: Default Pipeline
tags: [system, pipeline, dv2]
---

## Prose

The system default pipeline. Reacts to task lifecycle and spec events.
Ships two review rules (Dv2 Phase 2):

- **Per-task review** (`scope: task`) — on every `task.completed` whose
  task has a branch, spawn one reviewer task with a `discovered-from`
  edge to the reviewed task and attach a `task` gate to each downstream
  dependent so nothing downstream runs until the review completes.
- **Per-branch final review** — see Task 5 below (added in the same
  file). One final-reviewer task per branch, gathered via
  `ensure_task(dedup_key = "branch-review:<branch>")`, with per-task
  reviews wired as `blocks` edges into it.

## Rules

```json
{
  "rules": [
    {
      "id": "per-task-review",
      "on": "task.completed",
      "scope": "task",
      "when": {
        "expr": "event.task.branch_name != null && event.task.branch_name != ''"
      },
      "nodes": [
        {
          "id": "create-review",
          "command": "create_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "title": "Review: {{event.title}}",
            "description": "Reviewing task: {{event.task_id}}\nBranch: {{event.task.branch_name}}\nPR: {{event.task.pr_url}}\n\nRead the diff and either approve (close this task with a summary) or reject (call reopen_with_feedback on the reviewed task, then close this task).",
            "profile_id": "reviewer"
          },
          "output": {"as": "review"},
          "on_success": "link-discovered-from",
          "on_failure": "fail"
        },
        {
          "id": "link-discovered-from",
          "command": "add_dependency",
          "args": {
            "task_id": "{{node_outputs.review.task_id}}",
            "depends_on_task_id": "{{event.task_id}}",
            "dep_type": "discovered-from"
          },
          "on_success": "fetch-downstream",
          "on_failure": "fail"
        },
        {
          "id": "fetch-downstream",
          "command": "get_downstream_tasks",
          "args": {"task_id": "{{event.task_id}}"},
          "output": {"as": "downstream"},
          "on_success": "gate-downstream",
          "on_failure": "fail"
        },
        {
          "id": "gate-downstream",
          "for_each": "{{node_outputs.downstream.tasks}}",
          "as": "dep",
          "command": "gate_create",
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "task",
            "title": "Awaiting review of {{event.task_id}}",
            "await_id": "{{node_outputs.review.task_id}}",
            "waiter_task_ids": ["{{loop.dep.id}}"]
          },
          "on_success": "done",
          "on_failure": "fail"
        },
        {"id": "done", "terminal": "success"},
        {"id": "fail", "terminal": "failure"}
      ]
    }
  ]
}
```
```

Notes for the executor:
- Template substitution `{{event.task.branch_name}}` requires the pipeline engine to fetch the task row on `task.completed`. Phase 1's `runner_context.py` already does this — if it does not, extend `dispatch()` to hydrate `event.task` from `db.get_task(event.task_id)` before evaluation. Add a step to Task 4 if this is not already in place; grep `src/playbooks/runner_context.py` for `event.task`.
- The `when.expr` filter is evaluated deterministically (no LLM) using the existing playbook conditional evaluator; if that evaluator does not support this shape, encode the branch check as a first action node that returns `success` only when the branch is non-empty and route `on_failure` to a `done` terminal.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_pipeline_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/prompts/default_playbooks/default-pipeline.md tests/test_review_pipeline_rules.py
git commit -m "feat(pipeline): per-task review rule spawns reviewer + gates downstream"
```

---

### Task 5: Add per-branch final-review rule to the default pipeline playbook

**Files:**
- Modify: `src/prompts/default_playbooks/default-pipeline.md` — append a second rule (`scope: branch`) that uses `ensure_task` to keep one final-reviewer task per branch and wires each per-task review into it via `blocks`.
- Test: `tests/test_review_pipeline_rules.py` — extend with per-branch cases.

**Interfaces:**
- Consumes: `ensure_task(project_id, dedup_key, ...)` (Phase 1), `add_dependency` with `dep_type=blocks`, `gate_create` with `gate_type=pr-merged` (existing type, auto-resolved by `_sweep_resolve_pr_ci_gates` at `src/orchestrator/core.py:2339`).
- Produces: one final-reviewer task per (project, branch) pair, keyed `dedup_key = "branch-review:" + branch`. Every per-task review created by the Task 4 rule adds a `blocks` edge from itself to the branch's final-review task. Downstream dependents on the reviewed task additionally get a `pr-merged` gate awaiting the branch's PR.

- [ ] **Step 1: Extend tests**

Append to `tests/test_review_pipeline_rules.py`:

```python
@pytest.mark.asyncio
async def test_per_branch_review_ensures_one_task_per_branch(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = await pipeline_engine_factory(handler=h)
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="R", config={"needs_workspace": True, "read_only": True})
    await h.db.upsert_agent_profile(id="final-reviewer", name="F", config={"needs_workspace": True})

    ta = (await h.execute("create_task", {"project_id": "p", "title": "A", "profile_id": "worker"}))["task_id"]
    tb = (await h.execute("create_task", {"project_id": "p", "title": "B", "profile_id": "worker"}))["task_id"]
    await h.db.update_task(ta, branch_name="feature/shared")
    await h.db.update_task(tb, branch_name="feature/shared")

    await engine.dispatch("task.completed", {"task_id": ta, "project_id": "p", "title": "A"}, event_id="e-a")
    await engine.dispatch("task.completed", {"task_id": tb, "project_id": "p", "title": "B"}, event_id="e-b")

    tasks = await h.db.list_tasks(project_id="p")
    finals = [t for t in tasks if t.profile_id == "final-reviewer"]
    assert len(finals) == 1, "one final-review task per branch, coalesced by ensure_task"
    reviews = [t for t in tasks if t.profile_id == "reviewer"]
    assert len(reviews) == 2
    # Each per-task review blocks the final review.
    for r in reviews:
        edges = await h.db.get_dependencies_for_task(finals[0].id)
        assert any(e.depends_on_task_id == r.id and e.dep_type == "blocks" for e in edges)


@pytest.mark.asyncio
async def test_per_branch_review_gates_downstream_with_pr_merged(
    command_handler_factory, pipeline_engine_factory
):
    h = await command_handler_factory()
    engine = await pipeline_engine_factory(handler=h)
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="R", config={"needs_workspace": True, "read_only": True})
    await h.db.upsert_agent_profile(id="final-reviewer", name="F", config={"needs_workspace": True})
    t = (await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}))["task_id"]
    dep = (await h.execute("create_task", {"project_id": "p", "title": "Dep", "profile_id": "worker"}))["task_id"]
    await h.execute("add_dependency", {"task_id": dep, "depends_on_task_id": t, "dep_type": "blocks"})
    await h.db.update_task(t, branch_name="feature/x", pr_url="https://github.com/o/r/pull/9")
    await engine.dispatch("task.completed", {"task_id": t, "project_id": "p", "title": "T"})
    gates = await h.db.get_gates_for_task(dep)
    assert any(g["gate_type"] == "pr-merged" and g["await_id"] == "https://github.com/o/r/pull/9" for g in gates)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_pipeline_rules.py -v`
Expected: FAIL — the branch rule and pr-merged gate wiring are not present yet.

- [ ] **Step 3: Append the per-branch rule inside the `rules:` list of `src/prompts/default_playbooks/default-pipeline.md`**

```json
    {
      "id": "per-branch-final-review",
      "on": "task.completed",
      "scope": "branch",
      "when": {
        "expr": "event.task.branch_name != null && event.task.branch_name != ''"
      },
      "nodes": [
        {
          "id": "ensure-final",
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "branch-review:{{event.task.branch_name}}",
            "title": "Final review: {{event.task.branch_name}}",
            "description": "Final review for branch {{event.task.branch_name}}. Runs after every per-task review approves. Merge authority.",
            "profile_id": "final-reviewer"
          },
          "output": {"as": "final"},
          "on_success": "find-latest-review",
          "on_failure": "fail"
        },
        {
          "id": "find-latest-review",
          "command": "list_tasks",
          "args": {
            "project_id": "{{event.project_id}}",
            "profile_id": "reviewer",
            "discovered_from_task_id": "{{event.task_id}}",
            "status_in": ["DEFINED", "READY", "IN_PROGRESS"]
          },
          "output": {"as": "reviews"},
          "on_success": "wire-review-blocks-final",
          "on_failure": "fail"
        },
        {
          "id": "wire-review-blocks-final",
          "for_each": "{{node_outputs.reviews.tasks}}",
          "as": "rev",
          "command": "add_dependency",
          "args": {
            "task_id": "{{node_outputs.final.task_id}}",
            "depends_on_task_id": "{{loop.rev.id}}",
            "dep_type": "blocks"
          },
          "on_success": "fetch-downstream-branch",
          "on_failure": "fail"
        },
        {
          "id": "fetch-downstream-branch",
          "command": "get_downstream_tasks",
          "args": {"task_id": "{{event.task_id}}"},
          "output": {"as": "downstream"},
          "on_success": "gate-downstream-pr-merged",
          "on_failure": "fail"
        },
        {
          "id": "gate-downstream-pr-merged",
          "for_each": "{{node_outputs.downstream.tasks}}",
          "as": "dep",
          "when": {"expr": "event.task.pr_url != null && event.task.pr_url != ''"},
          "command": "gate_create",
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "pr-merged",
            "title": "Awaiting merge of {{event.task.branch_name}}",
            "await_id": "{{event.task.pr_url}}",
            "waiter_task_ids": ["{{loop.dep.id}}"]
          },
          "on_success": "done",
          "on_failure": "fail"
        },
        {"id": "done", "terminal": "success"},
        {"id": "fail", "terminal": "failure"}
      ]
    }
```

Executor notes:
- `list_tasks` currently exists but the `discovered_from_task_id` filter kwarg may not — grep `src/commands/task_commands.py` for `_cmd_list_tasks`. If unsupported, add the filter (small: join `task_dependencies` where `depends_on_task_id=<x>` AND `dep_type='discovered-from'`); write a one-off unit test and land it in this same task.
- `for_each` semantics + `when` on each iteration must match Phase 1's runner. If Phase 1 does not support per-iteration `when`, split the loop into two: one iteration that always runs the gate (with the PR-URL check moved to a synthetic gate-input rejection) — but only do this if a quick check confirms the missing feature.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_pipeline_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/prompts/default_playbooks/default-pipeline.md src/commands/task_commands.py tests/test_review_pipeline_rules.py
git commit -m "feat(pipeline): per-branch final review coalesces via ensure_task + pr-merged gates"
```

---

### Task 6: Rework loop — reopen cancels stale reviews; fresh completion fires fresh reactions

**Files:**
- Modify: `src/commands/task_commands.py` — extend `_cmd_reopen_with_feedback` (~line 1713) to cancel any open review tasks whose `discovered-from` edge points at the reopened task, before it flips the task to READY.
- Test: `tests/test_review_reopen_cascade.py`

**Interfaces:**
- Consumes: `db.list_tasks(project_id, ...)`, `db.get_dependencies_for_task(task_id)` (or the `depends_on` inverse — grep the queries mixin for the right method), `db.transition_task(id, TaskStatus.CANCELLED, ...)`. If no CANCELLED status exists — the enum lists 11 statuses and `TaskStatus.SKIPPED` or `FAILED` are the terminal candidates — resolve by choosing `FAILED` with a distinct context string (`"reopen_cascade:stale_review"`), which surfaces in the audit trail and is filterable in the dashboard. Do NOT invent a new status.
- Produces: after `reopen_with_feedback`, any open (non-terminal) review task with a `discovered-from` edge pointing at the reopened task is transitioned to FAILED with context `"reopen_cascade:stale_review"` and its `task` gate on downstream is auto-resolved by the sweep on its next tick (since the gate awaits the review's completion, and FAILED is a terminal status that the sweep already treats as satisfied — verify at `src/orchestrator/core.py:2320-2337`).

- [ ] **Step 1: Verify the `task` gate sweep treats FAILED as satisfying `await_id`**

Read `_sweep_resolve_task_gates` at `src/orchestrator/core.py:2320`. It currently checks `dep.status.value == "COMPLETED"`. **Design decision (locked here):** extend that check to also treat FAILED as satisfying — a task that FAILED is terminal and its gate waiters must not stall forever. Add this change as a step in this task; it is a one-liner but must land in the same commit as the reopen cascade to keep semantics coherent.

- [ ] **Step 2: Write failing test**

```python
# tests/test_review_reopen_cascade.py
import pytest

@pytest.mark.asyncio
async def test_reopen_cancels_stale_open_reviews(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="R", config={"needs_workspace": True, "read_only": True})

    t = (await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}))["task_id"]
    await h.db.update_task(t, branch_name="feature/t")
    await h.db.transition_task(t, "COMPLETED", context="test")

    r = (await h.execute("create_task", {"project_id": "p", "title": "Review: T", "profile_id": "reviewer"}))["task_id"]
    await h.execute("add_dependency", {"task_id": r, "depends_on_task_id": t, "dep_type": "discovered-from"})

    result = await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "please fix X"})
    assert result.get("reopened") == t

    review_after = await h.db.get_task(r)
    assert review_after.status.value == "FAILED"
    # Audit context is preserved for dashboard filtering.
    events = await h.db.list_events(task_id=r, event_type="task.transition")
    assert any("reopen_cascade" in (e.payload or "") for e in events)


@pytest.mark.asyncio
async def test_reopen_preserves_completed_reviews(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="R", config={"needs_workspace": True, "read_only": True})
    t = (await h.execute("create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}))["task_id"]
    await h.db.update_task(t, branch_name="feature/t")
    await h.db.transition_task(t, "COMPLETED", context="test")
    r = (await h.execute("create_task", {"project_id": "p", "title": "Review: T", "profile_id": "reviewer"}))["task_id"]
    await h.execute("add_dependency", {"task_id": r, "depends_on_task_id": t, "dep_type": "discovered-from"})
    await h.db.transition_task(r, "COMPLETED", context="test")  # review approved
    await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "fix Y"})
    review_after = await h.db.get_task(r)
    assert review_after.status.value == "COMPLETED", "completed reviews must be preserved as history"


@pytest.mark.asyncio
async def test_task_gate_sweep_resolves_on_failed_review(orchestrator_factory):
    orch = await orchestrator_factory()
    h = orch.command_handler
    await h.db.create_project(id="p", name="P")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    downstream = (await h.execute("create_task", {"project_id": "p", "title": "D", "profile_id": "worker"}))["task_id"]
    review = (await h.execute("create_task", {"project_id": "p", "title": "R", "profile_id": "worker"}))["task_id"]
    gate_id = await h.db.create_gate(
        project_id="p", gate_type="task",
        title="Awaiting review", await_id=review, waiter_task_ids=[downstream],
    )
    await h.db.transition_task(review, "FAILED", context="reopen_cascade:stale_review")
    await orch._sweep_gates()
    gate = await h.db.get_gate(gate_id)
    assert gate["status"] == "resolved", "task gate must resolve on FAILED await target"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_review_reopen_cascade.py -v`
Expected: FAIL — reopen does not cancel stale reviews yet; sweep does not treat FAILED as satisfying.

- [ ] **Step 4: Extend `_sweep_resolve_task_gates` in `src/orchestrator/core.py`**

At line 2334, change:

```python
            if getattr(dep.status, "value", dep.status) == "COMPLETED":
```

to:

```python
            status_val = getattr(dep.status, "value", dep.status)
            if status_val in ("COMPLETED", "FAILED"):
```

Adjust the `resolution` string to include the status:

```python
                await self._resolve_gate_and_emit(
                    gate["id"],
                    resolved_by="sweep:task",
                    resolution=f"{await_id}:{status_val}",
                )
```

- [ ] **Step 5: Extend `_cmd_reopen_with_feedback` in `src/commands/task_commands.py`**

Immediately after the `await self.db.log_event("reopen_with_feedback", ...)` call (~line 1773), before the return, add the stale-review cancellation block:

```python
        # Cancel stale open reviews of this task (Dv2 Phase 2 rework loop).
        # A review with a ``discovered-from`` edge pointing at the reopened
        # task is by construction gating downstream on THIS task's now-stale
        # completion.  Transition every such review that is not already
        # terminal to FAILED with a distinct context — the sweep resolves
        # any ``task`` gates awaiting it (see _sweep_resolve_task_gates).
        try:
            candidates = await self.db.list_tasks(project_id=task.project_id)
        except Exception:
            candidates = []
        terminal = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}
        for cand in candidates:
            status_val = getattr(cand.status, "value", cand.status)
            if status_val in terminal:
                continue
            edges = await self.db.get_dependencies_for_task(cand.id)
            if any(
                e.depends_on_task_id == task_id and e.dep_type == "discovered-from"
                for e in edges
            ):
                try:
                    await self.db.transition_task(
                        cand.id,
                        TaskStatus.FAILED,
                        context="reopen_cascade:stale_review",
                    )
                except Exception:
                    logger.warning(
                        "reopen_cascade: failed to cancel stale review %s", cand.id,
                        exc_info=True,
                    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_review_reopen_cascade.py -v`
Expected: PASS.

- [ ] **Step 7: Regression check on gate + reopen suites**

Run: `pytest tests/test_gate_queries.py tests/test_task_close_summary_enforcement.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/commands/task_commands.py src/orchestrator/core.py tests/test_review_reopen_cascade.py
git commit -m "feat(reopen): cancel stale reviews on reopen; sweep resolves task gates on FAILED"
```

---

### Task 7: pr-merged sweep integration test (existing sweep unblocks downstream after final-reviewer merges)

**Files:**
- Test: `tests/test_review_reopen_cascade.py` — append a case that exercises the existing `_sweep_resolve_pr_ci_gates` end-to-end against a `pr-merged` gate created by the branch rule.

**Interfaces:**
- Consumes: existing `_sweep_resolve_pr_ci_gates` at `src/orchestrator/core.py:2339`, which polls `GitManager.acheck_pr_merged`. Monkeypatch that helper to return `True` for a known PR URL and assert the downstream task's `is_blocked` flips to 0.
- Produces: no new code — only proof that the shipped rule wiring interoperates with the shipped sweep.

- [ ] **Step 1: Write the test**

Append to `tests/test_review_reopen_cascade.py`:

```python
@pytest.mark.asyncio
async def test_pr_merged_sweep_unblocks_downstream(orchestrator_factory, monkeypatch):
    orch = await orchestrator_factory()
    h = orch.command_handler
    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})

    downstream = (await h.execute("create_task", {"project_id": "p", "title": "D", "profile_id": "worker"}))["task_id"]
    pr = "https://github.com/o/r/pull/17"
    gate_id = await h.db.create_gate(
        project_id="p", gate_type="pr-merged",
        title="Awaiting merge",
        await_id=pr,
        waiter_task_ids=[downstream],
    )

    # Downstream is blocked before the merge.
    dt = await h.db.get_task(downstream)
    assert dt.is_blocked

    async def fake_check(checkout, url):
        assert url == pr
        return True
    monkeypatch.setattr(h.git, "acheck_pr_merged", fake_check)

    # Force the sweep interval so it actually runs.
    orch._last_gate_sweep = 0.0
    orch.config.work_graph.gate_sweep_interval_seconds = 1
    await orch._sweep_gates()

    gate = await h.db.get_gate(gate_id)
    assert gate["status"] == "resolved"
    dt2 = await h.db.get_task(downstream)
    assert not dt2.is_blocked
```

- [ ] **Step 2: Run test to verify it passes (no code change expected)**

Run: `pytest tests/test_review_reopen_cascade.py::test_pr_merged_sweep_unblocks_downstream -v`
Expected: PASS. If FAIL, the failure indicates that either Phase 1's `_poll_pr_merged` short-circuit (`src/orchestrator/approval.py:145`) or the sweep throttle interfered — read the failure and adjust the monkeypatch target to `orch._poll_pr_merged` returning `True` directly.

- [ ] **Step 3: Commit**

```bash
git add tests/test_review_reopen_cascade.py
git commit -m "test: pr-merged sweep unblocks downstream after branch merge"
```

---

### Task 8: End-to-end integration test — full chain

**Files:**
- Test: `tests/test_review_pipeline_e2e.py`

**Interfaces:**
- Consumes: everything shipped in Tasks 1-7. Uses `command_handler_factory` + `pipeline_engine_factory` + `orchestrator_factory` fixtures (mocking `git.acheck_pr_merged` and `git.amerge_pr`).
- Produces: proof that create → complete → review spawn → per-task-review approval → final-review spawn → merge → downstream unblock all wire together.

- [ ] **Step 1: Write the test**

```python
# tests/test_review_pipeline_e2e.py
import pytest

@pytest.mark.asyncio
async def test_full_review_chain_end_to_end(
    orchestrator_factory, pipeline_engine_factory, monkeypatch
):
    orch = await orchestrator_factory()
    h = orch.command_handler
    engine = await pipeline_engine_factory(handler=h)

    await h.db.create_project(id="p", name="P", workspace_path="/tmp/p")
    await h.db.upsert_agent_profile(id="worker", name="W", config={"needs_workspace": True})
    await h.db.upsert_agent_profile(id="reviewer", name="R", config={"needs_workspace": True, "read_only": True})
    await h.db.upsert_agent_profile(id="final-reviewer", name="F", config={"needs_workspace": True})

    worker_task = (await h.execute("create_task", {
        "project_id": "p", "title": "Do work", "profile_id": "worker",
    }))["task_id"]
    downstream = (await h.execute("create_task", {
        "project_id": "p", "title": "Depends", "profile_id": "worker",
    }))["task_id"]
    await h.execute("add_dependency", {
        "task_id": downstream, "depends_on_task_id": worker_task, "dep_type": "blocks",
    })

    # Worker "completes" with a branch + PR + summary.
    await h.db.update_task(worker_task, branch_name="feature/e2e", pr_url="https://github.com/o/r/pull/99")
    await h.db.transition_task(worker_task, "IN_PROGRESS", context="test")
    await h.execute("task_close", {
        "task_id": worker_task, "outcome": "success", "summary": "did the work",
    })

    # Pipeline reacts: one reviewer + one final-reviewer + gates.
    await engine.dispatch("task.completed", {
        "task_id": worker_task, "project_id": "p", "title": "Do work",
    }, event_id="e-1")

    tasks = await h.db.list_tasks(project_id="p")
    reviews = [t for t in tasks if t.profile_id == "reviewer"]
    finals = [t for t in tasks if t.profile_id == "final-reviewer"]
    assert len(reviews) == 1 and len(finals) == 1

    # Downstream is blocked (task gate + pr-merged gate).
    d = await h.db.get_task(downstream)
    assert d.is_blocked

    # Reviewer approves.
    await h.db.transition_task(reviews[0].id, "IN_PROGRESS", context="test")
    await h.execute("task_close", {
        "task_id": reviews[0].id, "outcome": "success", "summary": "LGTM",
    })

    # Sweep resolves the task gate; final-reviewer becomes READY.
    orch._last_gate_sweep = 0.0
    orch.config.work_graph.gate_sweep_interval_seconds = 1
    await orch._sweep_gates()
    final_after = await h.db.get_task(finals[0].id)
    assert not final_after.is_blocked

    # Final reviewer "merges" the PR.
    monkeypatch.setattr(h.git, "amerge_pr", lambda cp, url, method="squash": {
        "success": True, "sha": "f" * 40, "error": None,
    })
    monkeypatch.setattr(h.git, "acheck_pr_merged", lambda cp, url: True)
    await h.execute("pr_merge", {
        "project_id": "p", "pr_url": "https://github.com/o/r/pull/99",
    })
    await h.db.transition_task(finals[0].id, "IN_PROGRESS", context="test")
    await h.execute("task_close", {
        "task_id": finals[0].id, "outcome": "success", "summary": "merged fff",
    })

    # Sweep resolves pr-merged gate; downstream unblocks.
    orch._last_gate_sweep = 0.0
    await orch._sweep_gates()
    d2 = await h.db.get_task(downstream)
    assert not d2.is_blocked
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_review_pipeline_e2e.py -v`
Expected: PASS. If a fixture is missing (`orchestrator_factory`, `pipeline_engine_factory`), grep `tests/conftest.py` for the actual names and adjust.

- [ ] **Step 3: Commit**

```bash
git add tests/test_review_pipeline_e2e.py
git commit -m "test(e2e): full review chain — completion → review → final → merge → unblock"
```

---

## Self-Review

- **Spec coverage.** §7 per-task review rule → Task 4. §7 per-branch final review + merge authority → Tasks 3, 5, 1. §7 close-with-summary + commit capture → Task 2. §7 downstream `pr-merged` gates → Task 5 + Task 7. Rework loop (idempotency + stale review handling) → Task 6. Unit tests per rule action → Tasks 4, 5. Sweep test for `pr-merged` auto-resolve → Task 7. Integration test full chain → Task 8. All spec bullets addressed.
- **No placeholders.** Every code step ships complete code. Two executor-flexibility notes (Task 3 step 3 installer manifest; Task 4/5 `for_each`/`when` semantics) are conditional — grep-driven — because Phase 1 details might land in either form; the plan includes what to check and what the fix is either way, so the implementer has explicit direction.
- **Type consistency.** `ensure_task`, `create_task`, `add_dependency`, `gate_create`, `get_downstream_tasks`, `_cmd_reopen_with_feedback`, `_sweep_resolve_task_gates`, `_sweep_resolve_pr_ci_gates`, `amerge_pr`, `arev_parse`, `TaskStatus.FAILED` — used consistently across tasks. Rule ids (`per-task-review`, `per-branch-final-review`), profile ids (`reviewer`, `final-reviewer`), dedup key format (`branch-review:<branch>`), and event names (`task.completed`) match the spec.

---

## Open Questions

1. **Does Phase 1's runner hydrate `event.task` on `task.completed`?** Task 4 templates read `event.task.branch_name`. If not, the pipeline runner must fetch the task row; this plan calls the executor to verify via `grep src/playbooks/runner_context.py`, but the exact API is Phase 1's to define.
2. **`list_tasks` filter kwargs.** Task 5 relies on `discovered_from_task_id` + `status_in`. If these are not yet supported, the executor adds them; a follow-up test may be needed if Phase 1 defines them differently.
3. **Reviewer / final-reviewer allowed_tools names.** `gh_pr_view`, `gh_pr_diff`, `gh_run_view`, `git_log`, `git_diff`, `git_show`, `read_file` — the plan assumes these are canonical tool names in the codebase (or plugin-provided). Grep for `_cmd_git_diff` / `read_file` on execution; rename in the profile to match reality without changing semantics.
4. **Choice of terminal status for stale review cancellation.** Plan uses `FAILED` with `context="reopen_cascade:stale_review"`. If a `CANCELLED` status has been added since this plan was written, prefer that — it is more honest. The gate sweep extension in Task 6 should then include `CANCELLED` too.
