---
id: bugfix-pipeline
triggers:
  - type: task.created
    filter:
      task_type: BUGFIX
scope: agent-type:supervisor
---

# Bugfix Pipeline

Coordinate a streamlined code-then-QA workflow when a bugfix task is created.
Unlike the feature pipeline, this skips the code review stage — bugfixes are
typically small, targeted changes where the cost of a full review cycle
outweighs the risk. Go straight from coding to QA.

## Analyze the bugfix task

Read the triggering task's description, acceptance criteria, and any linked
context (error logs, issue references, failing test names, stack traces).
Determine the scope of the fix:

- Which files or modules are likely involved?
- Is there a reproducing test case, or does one need to be written?
- Does the task reference a prior failed task or QA result? If so, pull
  context from that task's output — the coding agent benefits from knowing
  what was already tried.

Check the workflow's project for any active workflows touching the same
files. If another workflow is in progress on overlapping code, note this
in the coding task's description so the agent can coordinate (rebase,
avoid conflicts).

## Create the coding task

Create a coding task using `create_task`:

- **Title:** the original bugfix task's title (or a concise summary if
  the original is too long)
- **Description:** include the full bug context — reproduction steps,
  error messages, affected files, and acceptance criteria from the
  triggering task. Instruct the agent to write or update a regression
  test that fails before the fix and passes after.
- **Agent type:** `coding`
- **Affinity:** if the triggering task references a prior task or failed
  workflow, prefer the agent that worked on it (`affinity_agent_id` with
  `affinity_reason: "context"`). That agent already has workspace state
  and conversation history relevant to this area of the codebase. If
  there is no prior context, do not set affinity — let the scheduler
  assign the best available agent.
- **Priority:** inherit from the triggering bugfix task. If the bugfix
  is marked critical or has a high priority, preserve that.
- **Dependencies:** none — this is the first task in the pipeline.

Record the coding task's ID in the workflow's `task_ids` and note the
assigned agent in `agent_affinity` for later stages.

## Create the QA task

Create a QA task using `create_task`:

- **Title:** "QA: {bugfix task title}"
- **Agent type:** `qa`
- **Description:** verify that the bugfix resolves the reported issue
  without introducing regressions. Specifically:
  - Run the full test suite (or the relevant subset if the project
    defines scoped test commands).
  - Confirm the regression test added by the coding agent passes.
  - Check that no previously passing tests now fail.
  - If the bug had a manual reproduction path, verify it through
    code inspection or integration tests.
  - Review the diff for unintended side effects — changes outside
    the scope of the fix, removed tests, or commented-out code.
- **Dependencies:** the coding task — the scheduler will not start
  QA until coding completes.
- **Priority:** one level higher than the coding task so QA is
  scheduled promptly once unblocked.

## Handle QA results

If QA passes (task completes with no issues):
  The bugfix is verified. If the project allows auto-merge and the
  coding task created a PR, auto-merge it. Otherwise, post a note
  to the project channel that the bugfix is ready for human merge.
  Mark the workflow as `completed`.

If QA fails:
  Check how many fix cycles have already occurred in this workflow.
  If this is the third QA failure, do not create another fix task —
  escalate by posting a note to the project channel explaining that
  the bugfix has failed QA three times and needs human intervention.
  Mark the workflow as `failed`.

  Otherwise, create a fix task:

  - **Title:** "Fix QA failures: {bugfix task title}"
  - **Agent type:** `coding`
  - **Description:** include the QA agent's failure report — which
    tests failed, what regressions were found, what side effects
    were identified. The coding agent should address these specific
    issues without re-implementing the entire fix.
  - **Affinity:** the agent that wrote the original fix
    (`affinity_agent_id` from the workflow's `agent_affinity` map,
    `affinity_reason: "context"`). That agent understands the fix
    approach and can iterate faster than a fresh agent.
  - **Dependencies:** the failed QA task.
  - **Priority:** same as the original coding task.

  Then create a new QA task that depends on the fix task. This
  re-enters the QA cycle. The fix-then-QA loop runs at most 3 times.

## Handle coding task failure

If the coding task itself fails (not a QA failure, but the coding
agent could not produce a fix):

  Check the failure reason. If it is transient (rate limit, timeout,
  agent crash), restart the coding task. Transient failures do not
  count toward any retry limit — the scheduler handles backoff.

  If it is a substantive failure (the agent could not figure out the
  fix, the codebase is in an unexpected state, merge conflicts
  prevent progress), mark the workflow as `failed` and post a note
  to the project channel with the failure details. Do not
  automatically retry substantive coding failures — they need human
  guidance or a different approach.
