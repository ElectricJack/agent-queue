---
id: feature-pipeline
triggers:
  - type: task.created
    filter:
      task_type: FEATURE
scope: agent-type:supervisor
---

# Feature Pipeline

Coordinate the full coding, review, and QA lifecycle when a feature task is
created. This is the primary coordination playbook for new feature work — it
builds a dependency DAG that flows from implementation through parallel review
and QA, with feedback loops when changes are needed.

Parallelism is not directed explicitly. Review and QA tasks both depend on the
coding task but not on each other, so the scheduler runs them concurrently when
agents are available. The playbook defines the structure; the scheduler
determines execution timing.

## Analyze the feature task

Read the triggering task's description, acceptance criteria, and any attached
context (linked specs, referenced files, project knowledge, prior exploration
results). Determine the scope and complexity of the feature:

- What modules or files are likely affected?
- Are there existing tests that cover the affected area?
- Does the task reference a spec document or design decision? If so, include
  the relevant sections in the coding task's description — the coding agent
  should not need to discover context that the triggering task already
  provides.
- Does the project have any active workflows touching overlapping files? If
  so, note this in the coding task's description so the agent can coordinate
  (rebase, avoid conflicts).

If the feature task's description is too vague to act on (no acceptance
criteria, no scope indication), post a note to the project channel requesting
clarification and pause the workflow. Do not create a coding task from an
underspecified feature request — vague tasks produce vague implementations
that waste review and QA cycles.

## Create the coding task

Create a coding task using `create_task`:

- **Title:** the original feature task's title (or a concise summary if the
  original is too long)
- **Description:** include the full feature context — requirements, acceptance
  criteria, relevant spec sections, affected files, and any design constraints
  from the triggering task. Instruct the agent to create a PR when the
  implementation is ready for review. The agent should write tests that cover
  the new functionality and ensure existing tests still pass.
- **Agent type:** `coding`
- **Affinity:** prefer an agent that has recently worked on this project
  (`affinity_reason: "context"`). Context continuity matters more than
  immediate availability for feature work — the agent that understands the
  codebase structure, conventions, and recent changes will produce better
  initial implementations, reducing review cycles. If no agent has recent
  project context, do not set affinity — let the scheduler assign the best
  available agent.
- **Priority:** inherit from the triggering feature task. If the feature is
  marked high priority, preserve that.
- **Dependencies:** none — this is the first task in the pipeline.

Record the coding task's ID in the workflow's `task_ids`. Record the assigned
agent in `agent_affinity` so later stages can route fix tasks back to this
agent.

Update the workflow's `current_stage` to `"coding"`.

## Create review and QA tasks

When the coding task completes and a PR has been created, create two tasks
that will run concurrently:

### Code review task

Create a code review task using `create_task`:

- **Title:** "Review: {feature task title}"
- **Agent type:** `code-review`
- **Description:** include the PR URL, the target branch, the list of changed
  files, and the diff summary. Specify review focus areas based on the feature
  scope:
  - Correctness against the acceptance criteria from the original feature task.
  - Adherence to project conventions and coding standards.
  - Test coverage — are the new tests sufficient? Are edge cases covered?
  - Security implications if the feature touches auth, user input, or data
    access patterns.
  - Backward compatibility if the feature changes public APIs or data formats.
  Include any project-specific review guidelines from project knowledge.
- **Affinity:** do not set affinity for the review task. The reviewer should
  be a different agent than the coding author for independent perspective.
  Note the coding agent's ID in the description so the scheduler avoids
  assigning the same agent.
- **Dependencies:** the coding task — the scheduler will not start the review
  until coding completes.
- **Priority:** one level higher than the coding task so review is scheduled
  promptly. Unblocking the review path reduces overall pipeline latency.

### QA task

Create a QA task using `create_task`:

- **Title:** "QA: {feature task title}"
- **Agent type:** `qa`
- **Description:** verify that the new feature works as specified and does
  not introduce regressions. Specifically:
  - Run the full test suite (or the relevant subset if the project defines
    scoped test commands).
  - Verify that the tests added by the coding agent cover the acceptance
    criteria from the original feature task.
  - Check that no previously passing tests now fail.
  - Review the diff for unintended side effects — changes outside the scope
    of the feature, removed tests, debug code left behind, or commented-out
    code.
  - If the feature has user-facing behavior, verify it through integration
    tests or code-level inspection of the interaction paths.
- **Dependencies:** the coding task — QA starts after coding completes. QA
  does not depend on the review task, and the review does not depend on QA.
  The scheduler will run them concurrently if agents are available.
- **Priority:** same as the review task.

Record both task IDs in the workflow's `task_ids`. Update the workflow's
`current_stage` to `"review_and_qa"`.

## Handle review results

When the review task completes, check the reviewer's verdict.

**If the review approves the PR (no changes requested):**

  Note the approval. The review path is clear. Check whether QA has also
  passed — if both review and QA are now complete with no issues, proceed
  to the completion stage. If QA is still running or has not started yet,
  wait for it.

**If the review requests changes:**

  Check how many review-fix cycles have already occurred in this workflow.

  If this is the third round of changes requested, do not create another
  fix task. Escalate by posting a note to the project channel explaining
  that the feature has been through three review cycles without reaching
  approval. Include a summary of the recurring issues from each review
  round. Mark the workflow as `needs_human`. A human should either merge
  with known issues, provide guidance to the coding agent, or re-scope the
  feature.

  Otherwise, create a fix task:

  - **Title:** "Address review feedback: {feature task title}"
  - **Agent type:** `coding`
  - **Description:** include the reviewer's specific feedback — which files
    need changes, what issues were identified, what improvements were
    requested. Structure the feedback as an actionable checklist so the
    coding agent can address each point systematically. Include the PR URL
    and branch name so the agent works on the correct branch.
  - **Affinity:** the agent that wrote the original implementation
    (`affinity_agent_id` from the workflow's `agent_affinity` map,
    `affinity_reason: "context"`). The original author understands the
    design intent and can iterate faster than a fresh agent.
  - **Dependencies:** the review task that requested changes.
  - **Priority:** same as the coding task — keep the feedback loop tight.

  Then create new review and QA tasks that both depend on the fix task. This
  re-enters the review and QA cycle. The fix-then-review loop runs at most
  3 times.

  Record all new task IDs in the workflow's `task_ids`.

## Handle QA results

When the QA task completes, check the result.

**If QA passes (no issues found):**

  Note the pass. The QA path is clear. Check whether the review has also
  approved — if both review and QA are now complete with no issues, proceed
  to the completion stage. If the review is still running or has not started
  yet, wait for it.

**If QA finds failures:**

  Check how many QA-fix cycles have already occurred in this workflow.

  If this is the third QA failure, do not create another fix task. Escalate
  by posting a note to the project channel explaining that the feature has
  failed QA three times and needs human investigation. Include a summary of
  the recurring failures from each QA round. Mark the workflow as `failed`.

  Otherwise, create a bugfix task:

  - **Title:** "Fix QA failures: {feature task title}"
  - **Agent type:** `coding`
  - **Description:** include the QA agent's failure report — which tests
    failed, what regressions were found, what side effects were identified.
    The coding agent should address these specific issues without
    re-implementing the entire feature. Include the PR URL and branch name.
  - **Affinity:** the agent that wrote the original implementation
    (`affinity_agent_id` from the workflow's `agent_affinity` map,
    `affinity_reason: "context"`). That agent understands the implementation
    approach and can diagnose failures faster than a fresh agent.
  - **Dependencies:** the failed QA task.
  - **Priority:** same as the original coding task.

  Then create a new QA task that depends on the bugfix task. This re-enters
  the QA cycle. The fix-then-QA loop runs at most 3 times.

  Note: QA failures create only a new QA task, not a new review task. The
  review cycle and QA cycle are independent feedback loops. If both review
  and QA request changes simultaneously, the fix task from one path should
  also address the other's feedback where possible — include both sets of
  feedback in the fix task description.

  Record all new task IDs in the workflow's `task_ids`.

## Completion

When both the review and QA paths have completed successfully (review
approved, QA passed, no outstanding fix tasks):

  If the project allows auto-merge and all required CI checks have passed,
  auto-merge the PR. Otherwise, post a note to the project channel
  indicating that the feature is approved, QA has passed, and the PR is
  ready for human merge. Include the PR URL.

  Mark the workflow as `completed`. Update the workflow's `current_stage`
  to `"done"`.

## Handle coding task failure

If the initial coding task fails (not a review or QA issue, but the coding
agent could not produce an implementation):

  Check the failure reason. If it is transient (rate limit, timeout, agent
  crash), restart the coding task. Transient failures do not count toward
  any retry limit — the scheduler handles backoff.

  If it is a substantive failure (the agent could not implement the feature,
  the codebase is in an unexpected state, merge conflicts prevent progress,
  the feature scope is unclear despite the initial analysis), mark the
  workflow as `failed` and post a note to the project channel with the
  failure details. Do not automatically retry substantive coding failures —
  they need human guidance or a re-scoped task.

## Handle review task failure

If a review task fails (the review agent could not complete its work, not
a negative review verdict):

  Check the failure reason. If it is transient, restart the review task.

  If it is substantive (the diff is too large for the agent's context window,
  the code is in a language the review agent doesn't support), post a note
  to the project channel explaining that automated review could not be
  completed. If QA has passed or passes subsequently, the feature can
  still proceed with human review. Mark the workflow as `needs_human`.

## Handle QA task failure

If a QA task fails (the QA agent could not complete its work, not a QA
finding):

  Check the failure reason. If it is transient, restart the QA task.

  If it is substantive (tests cannot be run, environment issues, missing
  dependencies), post a note to the project channel. If review has approved
  or approves subsequently, the feature can still proceed with manual QA.
  Mark the workflow as `needs_human`.

## Handle fix task failure

If a fix task (from either the review or QA feedback loop) fails:

  Check the failure reason. Transient failures are restarted as above.

  For substantive failures (the feedback is ambiguous, the requested changes
  conflict with other requirements, the agent cannot figure out how to
  implement the fix), post a note to the project channel with the failure
  details and the original feedback. Mark the workflow as `failed`. Do not
  retry substantive fix failures — they need human guidance.

## Handle PR closed externally

If the PR is closed or merged outside of this workflow (by a human or
another process), detect this and wind down gracefully. Cancel any pending
review, QA, or fix tasks that have not yet started. For tasks already in
progress, allow them to complete but do not create follow-up tasks from
their results. Mark the workflow as `completed` (if merged) or `cancelled`
(if closed without merging).
