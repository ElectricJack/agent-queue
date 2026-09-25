# Nightly GitHub issue triage

The reviewed `github-issue-triage` playbook applies only to the `agent-queue`
project bound to `ElectricJack/agent-queue`. It is shipped in
`src/prompts/reviewed_playbooks/github-issue-triage/` **inactive**. The project
supervisor imports and activates it after deployment; it must not be activated
while this change is being developed.

At about 02:00 in the daemon's local timezone, a `cron.02:00` event starts a
scan. The command reads open GitHub issues oldest first, excludes pull
requests and issues labeled `aq-triaged`, and files at most five investigation
tasks per local day. Each task is keyed by repository and issue number. The
command creates the durable task before adding the label. If labeling fails,
the next scan finds the same unlabelled issue, reuses the task, and repairs the
label. Replays count tasks already filed that day toward the five task cap.

The investigation is research only. Its worker reads the issue and relevant
code, reproduces cheaply where possible, then submits a markdown report to
the Reviews tab with the issue link and number. The report includes a summary,
likely cause with file and line evidence, proposed fix with files, scope, risk
and test plan, alternatives, and a recommendation. The worker uses a no-op
work outcome when closing its research task.

Jack's review controls the next step:

| Decision | Result |
| --- | --- |
| Approve | `review.decided` files or reuses a fix task tied to the approved revision. The fix task requires `Fixes #N` in its PR body. |
| Request changes | The review service creates a response task with the decision note and unresolved comments. The worker addresses every comment and resubmits. |
| Reject | The review service creates a response task. The playbook reads Jack's note and comments; an explicit imperative to close posts his reason and closes the issue. Otherwise the worker revises to his approach or resubmits with one clarifying question. Rejection does not close an issue by itself. |

The manual `aq github-issue close-rejected --review-id <id>` command accepts
only a response worker assigned to that rejected review and checks for Jack's
explicit request in his note or comments. The automated rule applies the same
check. The GitHub client marks the closing comment so retrying after a network failure
does not post it twice. All GitHub operations use the project's verified
repository binding and the shared App or existing-login credential path.

The project must have the GitHub issue write permission in its configured App
or existing `gh` credential. A failed scan leaves the playbook run failed and
the next nightly scan repairs any missing label. A failed approval-triggered
fix filing can be replayed; the issue and review identify one fix task.
