---
id: pr-merge-sweep
kind: pipeline
role: pr-merge-sweep
scope: project
enabled: true
triggers:
  - timer.30m
cooldown: 1500
---

# PR merge sweep

Every 30 minutes, ensure exactly one open "merge sweep" task exists for this
project (dedup key `pr-merge-sweep`; a sweep already running is reused, not
duplicated). The task runs on the `pr-merger` profile, which auto-merges every
open PR that merges cleanly (no test run), and for conflicting PRs resolves the
conflicts, runs only the targeted/area tests, fixes what they find, then merges.

```json
{
  "rules": [
    {
      "id": "sweep-open-prs",
      "on": "timer.30m",
      "entry": "ensure_sweep_task",
      "nodes": {
        "ensure_sweep_task": {
          "command": "ensure_task",
          "args": {
            "project_id": "agent-queue",
            "dedup_key": "pr-merge-sweep",
            "title": "Merge open PRs (sweep)",
            "description": "Batch-merge all open pull requests for this repo. Follow the pr-merger profile procedure exactly: skip drafts, do-not-merge/wip labels, and very fresh PRs; merge every MERGEABLE PR immediately with pr_merge (merge commit) WITHOUT running tests; for CONFLICTING PRs merge origin/main into the branch, resolve conflicts preserving both sides, then run ONLY the targeted tests for touched modules plus their area suite (-n auto), fix what they find, push, merge. Leave unsafe conflicts open with a PR comment. Close with the list of merged / conflict-resolved / skipped PRs (or 'no open PRs').",
            "profile_id": "pr-merger",
            "priority": 15
          },
          "output": {"as": "sweep"},
          "on_success": "route_sweep_task",
          "on_failure": "done"
        },
        "route_sweep_task": {
          "command": "task_route",
          "args": {
            "task_id": "{{outputs.sweep.task_id}}",
            "profile_id": "pr-merger",
            "intelligence_class": "deep-medium"
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    }
  ]
}
```
