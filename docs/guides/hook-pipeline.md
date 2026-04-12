---
tags: [hooks, automation, pipeline, deprecated]
---

# Hook Pipeline Guide

> **Deprecated:** Hooks and rules have been replaced by [[specs/design/playbooks|Playbooks]] — a more powerful system that supports multi-step directed graphs, accumulated context, conditional branching, and human-in-the-loop checkpoints. New automation should use playbooks. See the [[specs/design/playbooks|Playbook spec]] for details.
>
> The hook engine still works for existing hooks, but will be removed in a future release.

---

Hooks enable automated workflows that react to task lifecycle events or run
on schedules. See [[specs/hooks|Hooks spec]] and [[specs/rule-system|Rule System spec]] for implementation details. Each hook follows a simple pipeline:

```
trigger → render prompt → invoke LLM
```

The LLM invocation uses a full Supervisor with all tools available, so hooks
can create tasks, check status, send notifications — anything a human user
can do via Discord chat, a hook can do autonomously.

## Trigger Types

### Periodic

Fires on a timer, checked every orchestrator cycle (~5 seconds):

```json
{"type": "periodic", "interval_seconds": 7200}
```

### Event-Driven

Fires when a matching EventBus event arrives:

```json
{"type": "event", "event_type": "task.completed"}
```

Available events: `task.completed`, `task.failed`, `note.created`,
`note.updated`, `note.deleted`, `file.changed`, `folder.changed`

### Scheduled

Fires once at a specific epoch timestamp, then auto-deletes itself. Used
for deferred one-shot work (e.g. "remind me to check the deploy in
30 minutes"). Created via the `schedule_hook` command:

```json
{"type": "scheduled", "fire_at": 1711929600}
```

## Prompt Templates

Use `{{placeholder}}` syntax in the `prompt_template` field. Placeholders
are resolved before LLM invocation.

| Placeholder | Resolves To |
|-------------|-------------|
| `{{event}}` | Full event data as JSON |
| `{{event.field}}` | Specific event field (e.g., `{{event.task_id}}`) |

Since the Supervisor has full tool access, it can gather any context it needs
(run shell commands, read files, query the database, etc.) as part of its
tool-use loop. The prompt template should contain clear instructions for
what the Supervisor should do.

## LLM Configuration

Hooks use the system's default chat provider by default, but can override
the provider and model per hook:

```json
{
    "llm_config": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514"
    }
}
```

## Cooldown & Concurrency

- **`cooldown_seconds`** (per hook, default 3600): minimum time between runs
  for a single hook. Prevents rapid re-firing after events.
- **`max_concurrent_hooks`** (global, default 2): caps how many hooks can
  run simultaneously. Configured in `hook_engine.max_concurrent_hooks`.
- **Manual triggers** (`/fire-hook`): ignore cooldown, subject to concurrency cap.

## Hook Run Tracking

Every hook execution is recorded in the `hook_runs` database table with:
- `trigger_reason`: "periodic", "event:task_completed", or "manual"
- `status`: "running", "completed", "failed", or "skipped"
- `context_results`: JSON array of step outputs
- `skipped_reason`: why the LLM was skipped (if applicable)
- `prompt_sent`: the rendered prompt
- `llm_response`: the LLM's response
- `tokens_used`: estimated token count

## Examples

### Post-Completion Code Review

Automatically review code changes when a task completes and create follow-up
tasks if issues are found:

```json
{
    "name": "auto-review",
    "trigger": {"type": "event", "event_type": "task.completed"},
    "prompt_template": "Task {{event.task_id}} just completed in project {{event.project_id}}.\n\nLook up the task details, then get the git diff of the task branch against main. Review the changes for bugs, security issues, or code quality problems. If you find issues, create a bugfix task describing the problem and fix.",
    "cooldown_seconds": 60
}
```

### Periodic Health Monitor

Check system health every 2 hours and create an alert task if services are degraded:

```json
{
    "name": "health-monitor",
    "trigger": {"type": "periodic", "interval_seconds": 7200},
    "prompt_template": "Run a health check by executing 'curl -s http://localhost:8080/health' in the project workspace. If the response indicates any degraded or unhealthy components, create a task to investigate.",
    "cooldown_seconds": 3600
}
```

### Failure Pattern Detection

When a task fails, check if it's a recurring pattern and suggest fixes:

```json
{
    "name": "failure-analysis",
    "trigger": {"type": "event", "event_type": "task.failed"},
    "prompt_template": "Task {{event.task_id}} in project {{event.project_id}} just failed.\n\nLook up the task details and recent task history. Search project memory for related failures. Analyze whether this is a recurring failure pattern. If so, create a task to address the root cause.",
    "cooldown_seconds": 600
}
```

### Daily Summary Report

Generate a daily project status summary:

```json
{
    "name": "daily-summary",
    "trigger": {"type": "periodic", "interval_seconds": 86400},
    "prompt_template": "Generate a brief daily summary of project activity. Check the system status to see recent task results, token usage, and any events. Summarize: tasks completed vs failed, token usage trends, and any issues that need attention. Post the summary to the project channel.",
    "cooldown_seconds": 82800
}
```

### Test Gate After Completion

Run tests after a task completes and create a bugfix task if they fail:

```json
{
    "name": "test-gate",
    "trigger": {"type": "event", "event_type": "task.completed"},
    "prompt_template": "Task {{event.task_id}} just completed. Run the project's test suite in the workspace. If any tests fail, analyze the failures and create a bugfix task with specific fixes needed.",
    "cooldown_seconds": 120
}
```
