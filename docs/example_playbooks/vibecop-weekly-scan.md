---
id: vibecop-weekly-scan
triggers:
  - timer.168h
scope: system
cooldown: 3600
---

# VibeCop Weekly Scan

Scan all active project workspaces for code quality issues using the
vibecop static analysis tool. This playbook replaces the former
`@cron("0 6 * * 1")` hook on the VibeCop plugin (see playbooks spec §16).

## Discover projects

Use `list_projects` to get all projects. Filter to only ACTIVE projects
that have a workspace path configured. Skip archived or paused projects.

If no active projects with workspaces are found, stop — there is nothing
to scan.

## Scan each project

For each active project with a workspace path:

1. Run `vibecop_scan` on the workspace directory with the default severity
   threshold (warning). Do not use `diff_ref` — this is a full scan, not
   a diff-based check.

2. If the scan returns error-severity findings, create a task for the
   project using `create_task` with priority 5:
   - Title: "Fix N vibecop error(s) in PROJECT_NAME"
   - Description: Include the workspace path, a summary of the findings,
     and instructions to run `vibecop_scan` for full details.

3. Skip projects whose scan fails (network error, missing workspace, etc.)
   — log the failure but continue scanning remaining projects.

## Summary

After scanning all projects, post a concise summary of results. Include:
- Number of projects scanned
- Total findings by severity (error, warning, info)
- Any projects that were skipped due to errors

Do not post a summary if zero findings were detected across all projects
— avoid noise in the channel.
