---
id: github-issue-triage
kind: pipeline
role: github-issue-triage
scope: project:agent-queue
enabled: true
triggers:
  - cron.02:00
  - review.decided
---

# Nightly GitHub issue triage

This policy is scoped to project `agent-queue` and its bound repository
`ElectricJack/agent-queue`. The daemon's local-time `cron.02:00` trigger fires
once per day. The supervisor activates this reviewed bundle after deployment;
shipping it does not activate it.

The work is separated into an investigation, Jack's document review, and an
approved fix. The investigation worker researches and submits the report in
the Reviews tab, with the issue number and link. It makes no code changes.
The report contains a summary, likely cause with file:line evidence, proposed
fix with approach, files, scope, risk and test plan, alternatives, and a fix /
needs more info / close recommendation.

Jack's Approve decision files one fix task bound to the approved revision. That
task requires `Fixes #N` in its PR body. Request Changes gives the worker the
decision note and every unresolved comment, then asks for another revision.
Reject also creates a response task. It must inspect the note and comments:
an explicit request to close the issue may post Jack's reason and close it;
an alternative approach leads to a revised report or a clear fix instruction;
an ambiguous decision leads to one clarifying question in the resubmission.
Rejection never closes an issue by default.

## Rule: investigate-nightly

Every `cron.02:00` tick begins this rule. There is no guard.

1. Call `github_issue_triage` with `project_id` `agent-queue`. It reads the
   oldest open issues without `aq-triaged`, excluding pull requests. It files
   at most five new investigation tasks per local day, each with a stable issue
   key, before applying `aq-triaged`. A replay reuses any existing task and
   repairs a missing label; a failed task filing cannot label the issue. A
   `swept` outcome ends the rule; `rejected` or `runtime_error` fails it.

## Rule: file-approved-fix

Every `review.decided` event with `decision` `approve` begins this rule. The
command itself verifies the approved review, project, author task, and current
revision, so an unrelated approval or a stale replay is ignored.

1. Call `github_issue_fix_approved` with the event's `project_id`, `review_id`
   and `revision`. It files or reuses one fix task for the approved report and
   links it to the issue. `created`, `reused`, or `ignored` ends the rule;
   `rejected` or `runtime_error` fails it.

## Rule: close-explicit-rejection

Every `review.decided` event with `decision` `reject` begins this rule. A
rejection is feedback; this rule only handles Jack's explicit imperative to
close the issue. The review service has already created a response task so
non-closure feedback can be revised and resubmitted.

1. Call `github_issue_rejection` with the event's `project_id`, `review_id`
   and `revision`. It reads Jack's decision note and comments on the current
   revision. If he explicitly asked to close the issue, it posts his reason
   once and closes the issue; otherwise it leaves the issue open. `closed` or
   `ignored` ends the rule; `rejected` or `runtime_error` fails it.

## Failure handling, uniformly

There is no immediate retry. A failed step leaves a failed run visible to the
supervisor. The next scheduled scan or replay starts from durable tasks and
GitHub labels. This bundle is shipped inactive; only the supervisor may
activate it after deployment.
