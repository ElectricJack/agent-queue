# GitHub Actions Integration for Agent-Queue: Research Findings

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

## Executive Summary

This document explores how to integrate GitHub Actions CI/CD feedback into the agent-queue system. The goal is to surface build/test/deploy status in Discord alongside existing task management, helping users track whether agent-produced code passes CI checks before or after PR creation.

Three integration approaches are feasible, listed in order of implementation complexity:

1. **Polling via `gh` CLI** (simplest, extends existing patterns)
2. **Periodic hook with `gh` API calls** (no code changes, uses existing hook infrastructure)
3. **Webhook receiver** (real-time, requires new HTTP endpoint infrastructure)

---

## 1. GitHub Actions API Surface

### Key Endpoints (REST API v3)

All endpoints are accessible via the `gh api` CLI command, which handles authentication automatically — matching the existing `gh`-based approach in `git/manager.py`.

#### Workflow Runs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/repos/{owner}/{repo}/actions/runs` | GET | List workflow runs (filterable by branch, status, event) |
| `/repos/{owner}/{repo}/actions/runs/{run_id}` | GET | Get a specific run |
| `/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` | GET | List jobs within a run |
| `/repos/{owner}/{repo}/actions/runs/{run_id}/logs` | GET | Download run logs (zip) |
| `/repos/{owner}/{repo}/actions/runs/{run_id}/rerun` | POST | Re-run a workflow |

**Key query parameters for listing runs:**
- `branch` — filter by branch name (critical: matches task branch names)
- `status` — `queued`, `in_progress`, `completed`
- `event` — `push`, `pull_request`, `workflow_dispatch`, etc.
- `per_page` — pagination (default 30, max 100)

#### Check Runs & Check Suites (PR-level)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/repos/{owner}/{repo}/commits/{ref}/check-runs` | GET | List check runs for a commit SHA |
| `/repos/{owner}/{repo}/commits/{ref}/check-suites` | GET | List check suites for a commit SHA |

**Key fields in check run response:**
- `status`: `queued`, `in_progress`, `completed`
- `conclusion`: `success`, `failure`, `neutral`, `cancelled`, `skipped`, `timed_out`, `action_required`
- `name`: workflow/job name
- `html_url`: link to the run in GitHub UI

#### Practical `gh` CLI Examples

```bash
# List runs for a specific branch
gh api repos/{owner}/{repo}/actions/runs \
  --jq '.workflow_runs[] | {id, status, conclusion, name: .name, branch: .head_branch}' \
  -f branch=wise-quest/some-task-branch

# Get check status for a PR
gh pr checks <pr_url> --json name,state,conclusion

# Get detailed check runs for a commit
gh api repos/{owner}/{repo}/commits/{sha}/check-runs \
  --jq '.check_runs[] | {name, status, conclusion}'

# Re-run failed workflows
gh api repos/{owner}/{repo}/actions/runs/{run_id}/rerun -X POST
```

**Important:** `gh pr checks` is the highest-value command — it returns all CI check statuses for a PR in one call, perfectly mapping to the existing task→PR relationship.

---

## 2. Webhook Events for Real-Time Notifications

### Relevant GitHub Webhook Events

#### `workflow_run` Event
- **Actions:** `requested`, `in_progress`, `completed`
- **Key payload fields:**
  - `action` — trigger type
  - `workflow_run.conclusion` — `success`, `failure`, `cancelled`, etc.
  - `workflow_run.head_branch` — branch name (maps to task branch)
  - `workflow_run.head_sha` — commit SHA
  - `workflow_run.pull_requests[]` — associated PRs
  - `workflow_run.name` — workflow name
  - `workflow_run.html_url` — link to run

#### `check_run` Event
- **Actions:** `created`, `completed`, `rerequested`
- **Key payload fields:**
  - `check_run.conclusion` — `success`, `failure`, etc.
  - `check_run.pull_requests[]` — associated PRs
  - `check_run.name` — check name

#### `check_suite` Event
- **Actions:** `completed`, `requested`, `rerequested`
- **Key payload fields:**
  - `check_suite.conclusion` — aggregate result
  - `check_suite.pull_requests[]` — associated PRs

### Webhook Setup Requirements

To receive webhooks, the agent-queue would need:

1. **A publicly accessible HTTP endpoint** — Either:
   - Extend the existing `HealthCheckServer` (in `src/health.py`) to accept POST requests
   - Use a webhook relay service (e.g., smee.io for development, ngrok, or a cloud function)
   - Deploy a small sidecar that forwards to the daemon

2. **Webhook secret verification** — HMAC-SHA256 signature validation on incoming payloads

3. **Webhook registration** — Via GitHub repository settings or the API:
   ```bash
   gh api repos/{owner}/{repo}/hooks -X POST \
     -f url=https://your-endpoint/webhooks/github \
     -f content_type=json \
     -f secret=YOUR_SECRET \
     -f events[]=workflow_run \
     -f events[]=check_run
   ```

### Webhook vs. Polling Trade-offs

| Factor | Polling (`gh` CLI) | Webhooks |
|--------|-------------------|----------|
| **Latency** | 30-60s (configurable) | Near real-time (~1-5s) |
| **Complexity** | Low (extends existing pattern) | Moderate (new HTTP endpoint) |
| **Reliability** | High (self-healing on restart) | Needs retry/replay mechanism |
| **Network** | Outbound only (NAT-friendly) | Requires inbound reachability |
| **Rate limits** | 5,000 req/hour (authenticated) | No API rate consumption |
| **Infrastructure** | None (uses existing `gh` auth) | Needs public URL + secret management |

**Recommendation:** Start with polling, add webhooks later if latency matters.

---

## 3. Integration Approaches

### Approach A: Extend `_check_awaiting_approval()` with CI Status (Recommended First Step)

**Where:** `src/orchestrator.py`, `src/git/manager.py`

The existing `_check_awaiting_approval()` loop already polls PR status every 60 seconds. This is the natural place to also check CI status for tasks with PRs.

**New method in `GitManager`:**

```python
async def acheck_pr_ci_status(self, checkout_path: str, pr_url: str) -> dict:
    """Check CI/check run status for a PR via `gh pr checks`.

    Returns dict with:
      - all_passed: bool
      - all_completed: bool
      - checks: list of {name, status, conclusion}
      - summary: str (human-readable)
    """
    result = subprocess.run(
        ["gh", "pr", "checks", pr_url, "--json", "name,state,conclusion,link"],
        cwd=checkout_path,
        capture_output=True, text=True,
        env=self._SUBPROCESS_ENV,
        timeout=self._GIT_TIMEOUT,
    )
    # Parse and return structured result
```

**New orchestrator behavior:**

```python
async def _check_ci_status_for_task(self, task: Task) -> None:
    """Check CI status and notify on state changes."""
    ci = await self.git.acheck_pr_ci_status(checkout_path, task.pr_url)

    # Store last known CI state in task_context to detect transitions
    prev = await self.db.get_task_context_by_type(task.id, "ci_status")

    if ci["all_completed"] and not ci["all_passed"]:
        # CI failed — notify in Discord with failure details
        await self._notify_channel(
            f"❌ **CI Failed:** Task `{task.id}` — {task.title}\n"
            f"{ci['summary']}\n"
            f"PR: {task.pr_url}",
            project_id=task.project_id,
        )
        # Optionally: auto-reopen task for fixes
    elif ci["all_passed"] and (not prev or prev != "passed"):
        await self._notify_channel(
            f"✅ **CI Passed:** Task `{task.id}` — {task.title}\n"
            f"PR: {task.pr_url}",
            project_id=task.project_id,
        )

    # Persist current state
    await self.db.add_task_context(task.id, "ci_status", ci)
```

**Advantages:**
- Extends existing polling pattern — minimal new code
- Uses existing `gh` authentication
- Naturally throttled by existing 60s interval
- Discord notifications use existing `_notify_channel()`
- CI state stored in `task_context` (existing table)

**Estimated effort:** ~100 lines of code across `manager.py` + `orchestrator.py`

---

### Approach B: Hook-Based CI Monitoring (Zero Code Changes)

**Where:** Database only — create a hook via `/create-hook`

The existing hook infrastructure already supports everything needed:

```json
{
  "name": "ci-monitor",
  "trigger_type": "periodic",
  "interval_seconds": 120,
  "project_id": "agent-queue",
  "context_steps": [
    {
      "type": "shell",
      "command": "gh pr list --repo ElectricJack/agent-queue --json number,title,headRefName,statusCheckRollup --limit 10"
    },
    {
      "type": "db_query",
      "query": "awaiting_approval_tasks"
    }
  ],
  "prompt": "You are a CI monitor. Compare the GitHub PR check statuses (step_0) with the tasks awaiting approval (step_1). For any task whose PR has failing CI checks, notify the project channel with details about what failed. For tasks with newly passing checks, send a success notification. Only report state CHANGES — do not repeat notifications for checks you've already reported."
}
```

**Advantages:**
- Zero code changes required
- Can be created and modified at runtime via Discord commands
- LLM interprets results intelligently (can summarize, deduplicate, etc.)
- Full tool access — can create follow-up tasks, add context, etc.

**Disadvantages:**
- Token cost per invocation (LLM call every 2 minutes)
- Less deterministic than code-based approach
- Requires adding `awaiting_approval_tasks` to `NAMED_QUERIES` in `hooks.py`

---

### Approach C: Webhook Receiver (Future Enhancement)

**Where:** New module `src/webhooks.py` + extend `src/health.py`

Extend the existing `HealthCheckServer` to accept POST requests on a `/webhooks/github` path:

```python
# In health.py or new webhooks.py
async def _handle_webhook(self, reader, writer, body: bytes) -> None:
    """Process incoming GitHub webhook payload."""
    # 1. Verify HMAC-SHA256 signature
    # 2. Parse event type from X-GitHub-Event header
    # 3. Extract branch/PR info
    # 4. Map to task via branch_name or pr_url
    # 5. Emit EventBus event: "ci.completed", "ci.failed"
    # 6. Return 200 OK
```

**EventBus integration:**

```python
# New events:
await self.event_bus.emit("ci.completed", {
    "task_id": task.id,
    "conclusion": "success",
    "workflow_name": "CI",
    "run_url": "https://github.com/...",
})

await self.event_bus.emit("ci.failed", {
    "task_id": task.id,
    "conclusion": "failure",
    "workflow_name": "CI",
    "failed_jobs": ["test", "lint"],
    "run_url": "https://github.com/...",
})
```

**Hook triggers for event-driven automation:**

```json
{
  "name": "ci-failure-handler",
  "trigger_type": "event",
  "trigger_event": "ci.failed",
  "prompt": "CI has failed for task {{event.task_id}}. Failed jobs: {{event.failed_jobs}}. Analyze the failure and decide: should we create a fix task, reopen the original task, or just notify the user?"
}
```

**Infrastructure requirements:**
1. Public URL (reverse proxy, tunnel, or cloud deployment)
2. Webhook secret in config (new config field: `github.webhook_secret`)
3. New config section:
   ```yaml
   github:
     webhook_secret: ${GITHUB_WEBHOOK_SECRET}
     webhook_path: /webhooks/github
   ```

**Estimated effort:** ~300 lines (webhook handler, signature verification, event mapping, config)

---

## 4. Discord Surfacing Options

### Option 1: Notifications in Task Threads

When CI status changes, post to the agent execution thread for that task:

```
✅ CI Passed — All 3 checks passed for branch `wise-quest/fix-login-bug`
  • build (12s) — success
  • test (45s) — success
  • lint (8s) — success
```

```
❌ CI Failed — 1 of 3 checks failed for branch `wise-quest/fix-login-bug`
  • build (12s) — success
  • test (45s) — ❌ failure  ← [View Logs](https://github.com/...)
  • lint (8s) — success
```

**Implementation:** Use existing `_send_to_thread()` or `_notify_channel()` patterns.

### Option 2: Project Channel Updates

Post CI summaries to the project's Discord channel as embeds:

```python
embed = discord.Embed(
    title="CI Status Update",
    color=0x28a745 if passed else 0xcb2431,
    description=f"Task: `{task.id}` — {task.title}",
)
embed.add_field(name="Branch", value=task.branch_name)
embed.add_field(name="PR", value=f"[#{pr_number}]({task.pr_url})")
embed.add_field(name="Checks", value=checks_summary)
```

### Option 3: New `/ci-status` Slash Command

Add a command to query CI status on demand:

```
/ci-status task:wise-quest
```

Returns:
```
CI Status for task `wise-quest` (branch: wise-quest/fix-login-bug)
PR: #42 — https://github.com/owner/repo/pull/42

✅ build      — success (12s)
❌ test       — failure (45s)  [View Logs]
✅ lint       — success (8s)

Overall: FAILING (1/3 checks failed)
```

**Implementation:** New command in `command_handler.py` + slash command in `discord/commands.py`. Delegates to `GitManager.acheck_pr_ci_status()`.

### Option 4: Status Enrichment in Existing Commands

Enhance `/status` and `/get-task` to include CI status alongside existing task info:

```
Task: wise-quest — Fix login bug
Status: AWAITING_APPROVAL
PR: https://github.com/owner/repo/pull/42
CI: ✅ All checks passed (3/3)
```

This is the least disruptive — just adds a field to existing embeds.

---

## 5. Automation Possibilities

### Auto-Reopen on CI Failure

When CI fails on a task's PR, automatically reopen the task with failure context:

```python
if ci_failed and task.status == TaskStatus.AWAITING_APPROVAL:
    # Attach failure logs as task context
    await self.db.add_task_context(task.id, type="ci_failure", content={
        "failed_checks": [...],
        "log_url": "...",
        "timestamp": "...",
    })
    # Reopen task for the agent to fix
    await command_handler.execute("reopen_with_feedback", {
        "task_id": task.id,
        "feedback": f"CI failed: {failure_summary}. Please fix the failing checks.",
    })
```

This leverages the existing `reopen_with_feedback` command (already stores feedback as `task_context`).

### Auto-Merge on CI Pass

For tasks that don't require manual approval but do require CI to pass:

```python
if ci_passed and task.auto_merge_on_ci:
    await git.amerge_pr(checkout_path, task.pr_url)
    await self.db.transition_task(task.id, TaskStatus.COMPLETED, context="ci_auto_merge")
```

This would need a new task field or project-level setting (`auto_merge_on_ci_pass`).

### CI Status in Task Dependencies

Block downstream tasks until upstream tasks' PRs pass CI:

```python
# In _check_defined_tasks(), when evaluating dependencies:
if dep_task.status == TaskStatus.AWAITING_APPROVAL:
    ci = await self._get_cached_ci_status(dep_task)
    if ci and not ci["all_passed"]:
        # Don't promote dependent task to READY yet
        continue
```

---

## 6. Implementation Roadmap

### Phase 1: CI Status Polling (Minimal, Non-Breaking)

**Scope:** Add `gh pr checks` polling to the existing approval loop.

1. Add `check_pr_ci_status()` to `GitManager` (~30 lines)
2. Add `_check_ci_for_awaiting_tasks()` to `Orchestrator` (~50 lines)
3. Store CI state in `task_context` table (existing infrastructure)
4. Send Discord notifications on CI state transitions (~20 lines)
5. Add a `ci_status` field to task embeds in `discord/embeds.py`

**Dependencies:** None — uses existing `gh` CLI auth and infrastructure.
**Risk:** Low — additive only, no changes to existing behavior.
**API rate impact:** ~1 `gh` call per AWAITING_APPROVAL task per 60s cycle.

### Phase 2: Discord Commands & UI

1. Add `/ci-status` slash command
2. Enrich `/get-task` and `/status` with CI info
3. Add "Re-run CI" button to task notification embeds

### Phase 3: Automated CI Failure Response

1. Auto-reopen tasks on CI failure (configurable per project)
2. Attach failure context to reopened tasks
3. Add `auto_merge_on_ci_pass` project/task setting
4. Auto-merge PRs when CI passes (for non-approval-required tasks)

### Phase 4: Webhook Integration (Optional, For Real-Time)

1. Extend `HealthCheckServer` with POST handler for `/webhooks/github`
2. Add HMAC signature verification
3. Map webhook payloads to EventBus events (`ci.completed`, `ci.failed`)
4. Create event-driven hooks for CI-triggered automations
5. Add `github.webhook_secret` config field

---

## 7. Existing Infrastructure Leverage

The agent-queue system already has all the building blocks needed:

| Need | Existing Infrastructure |
|------|------------------------|
| GitHub API calls | `gh` CLI (authenticated, in `GitManager`) |
| Periodic polling | `_check_awaiting_approval()` loop (60s cycle) |
| State persistence | `task_context` table (arbitrary JSON per task) |
| Discord notifications | `_notify_channel()`, embeds, thread posting |
| Event-driven automation | `EventBus` + `HookEngine` |
| Background jobs | `asyncio.Task` management in orchestrator |
| HTTP server | `HealthCheckServer` (extensible for webhooks) |
| Shell commands | Hook `shell` context steps (for `gh` CLI) |
| Configuration | YAML config with env var substitution |
| Task reopening | `reopen_with_feedback` command |

**No new dependencies** are required for Phase 1-3. Phase 4 (webhooks) only needs the `hmac` stdlib module for signature verification.

---

## 8. Rate Limit Considerations

- **GitHub API:** 5,000 requests/hour for authenticated users (via `gh`)
- **`gh pr checks`** counts as 1 API call per invocation
- With 10 concurrent AWAITING_APPROVAL tasks polled every 60s: ~600 calls/hour (well within limits)
- **Mitigation:** Cache CI status in `task_context`; only call API when status was previously non-terminal
- **Webhook approach** consumes zero API rate limit for receiving events

---

## 9. Open Questions

1. **Should CI failure auto-reopen tasks?** This is powerful but could cause infinite loops if CI issues are environmental rather than code-related. Recommend: configurable per project, with max retry count.

2. **Which CI events matter most?** For most users, `completed` (with conclusion) is sufficient. `in_progress` is nice-to-have for long-running workflows but adds noise.

3. **Should CI status block PR merges via the system?** The system could refuse to auto-merge until CI passes, but GitHub's branch protection rules already handle this. Avoid duplicating GitHub's native functionality.

4. **Token budget for hook-based approach?** If using Approach B (LLM hook), each invocation costs ~1-2K tokens. At 2-minute intervals, that's ~720 calls/day (~1M tokens). Code-based polling (Approach A) has zero token cost.

5. **Multi-repo support?** Projects can have multiple repos. CI monitoring should work per-repo, keyed by the task's branch name and the repo it was pushed to. The `workspace_path` already provides this mapping.
