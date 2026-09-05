---
id: pr-merger
name: "PR Merger"
description: "Batch-merges open pull requests: auto-merges clean ones without running tests; resolves conflicts, then runs targeted + area tests and fixes what they find before merging the rest."
tags: [profile, agent-type, merger]
---

## Config
```json
{
  "harness": "codex",
  "lifecycle": "task",
  "needs_workspace": true,
  "default_class": "deep-medium",
  "workspaces": ["project-repo"]
}
```

## Capabilities
```json
{
  "harness_tools": [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite", "Skill"
  ],
  "aq_commands": [
    "get_task", "task_close", "task_heartbeat", "task_comment", "pr_merge"
  ],
  "plugin_tools": []
}
```

## MCP Servers
```json
[]
```
