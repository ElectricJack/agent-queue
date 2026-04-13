---
id: review-cycle
triggers:
  - type: git.pr.created
scope: agent-type:supervisor
---

# Review Cycle

Coordinate code review and feedback iteration when a pull request is created.
This playbook assigns a reviewer, manages the review-fix loop when changes are
requested, and handles the final disposition of the PR (merge or escalate).

Unlike the feature pipeline — which orchestrates the full coding → review → QA
sequence from task creation — this playbook enters the picture later, when a PR
already exists. It covers the review-and-iterate portion of the workflow.

## Analyze the pull request

Read the PR metadata — title, description, changed files, diff size, target
branch, and the author (which agent or human created it). Check if this PR
was created by a task within an existing workflow. If so, that workflow already
manages the review stage and this playbook should not create a duplicate review.
Exit early if the PR is already covered by a coordination workflow.

Determine the scope and risk of the change:

- How many files are changed? How large is the diff?
- Does the change touch high-risk areas (database migrations, auth, billing,
  public API contracts, configuration)?
- Is this a single-commit fix or a multi-commit feature branch?
- Does the project have specific review guidelines in its project knowledge
  or playbook overrides?

Use this assessment to decide review depth. Small, low-risk changes (typo
fixes, documentation, dependency bumps) can use a lighter review. Large or
high-risk changes warrant thorough review with explicit focus areas.

## Create the review task

Create a code review task using `create_task`:

- **Title:** "Review PR: {PR title}" (truncate if the PR title is long)
- **Agent type:** `code-review`
- **Description:** include the PR URL, the target branch, the list of changed
  files, and the diff summary. Specify what the reviewer should focus on based
  on the risk assessment:
  - For high-risk changes: correctness, security implications, backward
    compatibility, test coverage, edge cases.
  - For standard changes: code quality, adherence to project conventions,
    test presence, clarity of implementation.
  - For low-risk changes: quick sanity check — does the change do what the
    title says, and does it introduce any obvious problems?
  Include any relevant project review guidelines pulled from project knowledge.
- **Affinity:** do not set affinity for the review task — the reviewer should
  be a different agent than the author for independent perspective. If the PR
  author is known, note this in the description so the scheduler avoids
  assigning the same agent.
- **Priority:** one level higher than the default so reviews are scheduled
  promptly. Unblocking PRs reduces cycle time for the whole pipeline.
- **Dependencies:** none — the PR already exists, so review can start
  immediately.

Record the review task's ID in the workflow's `task_ids`. Record the PR
author's agent ID in `agent_affinity` so fix tasks can be routed back to
the original author.

## Handle review results

When the review task completes, check the reviewer's verdict.

**If the review approves the PR (no changes requested):**

  The review cycle is complete. If the project allows auto-merge and all
  required checks have passed, merge the PR. Otherwise, post a note to the
  project channel indicating the PR is approved and ready for human merge.
  Mark the workflow as `completed`.

**If the review requests changes:**

  Check how many review-fix cycles have already occurred in this workflow.

  If this is the third round of changes requested, do not create another
  fix task. Escalate by posting a note to the project channel explaining
  that the PR has been through three review cycles without reaching
  approval. Include a summary of the recurring issues from each review
  round. Mark the workflow as `needs_human`. A human should either merge
  with known issues, provide guidance to the author, or close the PR.

  Otherwise, create a fix task:

  - **Title:** "Address review feedback: {PR title}"
  - **Agent type:** `coding`
  - **Description:** include the reviewer's specific feedback — which files
    need changes, what issues were identified, what improvements were
    requested. Structure the feedback as an actionable checklist so the
    coding agent can address each point. Include the PR URL and branch
    name so the agent works on the correct branch.
  - **Affinity:** the agent that authored the original PR
    (`affinity_agent_id` from the workflow's `agent_affinity` map,
    `affinity_reason: "context"`). The original author understands the
    design intent and can iterate faster than a fresh agent.
  - **Dependencies:** the review task that requested changes.
  - **Priority:** same as the review task — keep the feedback loop tight.

  Then create a new review task that depends on the fix task. This re-enters
  the review cycle. The fix-then-review loop runs at most 3 times.

  Record both new task IDs in the workflow's `task_ids`.

**If the review identifies blocking issues (security vulnerability, data
loss risk, architectural concern):**

  Do not create a fix task. Post a detailed note to the project channel
  with the blocking issue description, affected code locations, and the
  reviewer's assessment of severity. Mark the workflow as `blocked`. These
  issues require human judgment before proceeding — automated fix attempts
  on security or architectural problems risk making things worse.

## Handle review task failure

If the review task itself fails (not a negative review, but the review agent
could not complete its work):

  Check the failure reason. If it is transient (rate limit, timeout, agent
  crash), restart the review task. Transient failures do not count toward
  any retry limit — the scheduler handles backoff.

  If it is a substantive failure (the diff is too large for the agent's
  context window, the code is in a language the review agent doesn't
  support, the PR references external systems the agent can't access),
  post a note to the project channel explaining that automated review
  could not be completed and manual review is needed. Mark the workflow
  as `failed`.

## Handle fix task failure

If a fix task fails (the coding agent could not address the review feedback):

  Check the failure reason. Transient failures are restarted as above.

  For substantive failures (the feedback is ambiguous, the requested changes
  conflict with other requirements, the agent cannot figure out how to
  implement the fix), post a note to the project channel with the failure
  details and the original review feedback. Mark the workflow as `failed`.
  Do not retry substantive coding failures — they need human guidance.

## Handle PR closed externally

If the PR is closed or merged outside of this workflow (by a human or
another process), the workflow should detect this and wind down gracefully.
Cancel any pending review or fix tasks that have not yet started. For tasks
already in progress, allow them to complete but do not create follow-up
tasks from their results. Mark the workflow as `completed` (if merged) or
`cancelled` (if closed without merging).
