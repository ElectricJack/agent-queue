Investigate GitHub issue #{{number}}: {{title}}

Issue: {{url}}

Research only; do not change code or open a PR for this task. Read the issue and relevant code; reproduce it when cheap. Write a markdown report with: summary, likely cause with file:line evidence, proposed fix (approach, files, scope, risks and test plan), alternatives, and recommendation (fix, needs more info, or close). Submit the report to the Reviews tab with `aq review submit --task-id <this-task-id> --kind other --title 'GitHub issue #{{number}} investigation' --file <report.md>`. The report must link this issue. Close this research task with the no-op work outcome so it does not produce a code PR.

If Jack requests changes, address every comment and resubmit. A rejection is feedback unless Jack explicitly asks to close the issue: follow his alternative fix approach or ask one clarifying question in a revised report. Only use `aq github-issue close-rejected` if Jack explicitly requested closure.