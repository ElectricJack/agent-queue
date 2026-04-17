---
id: task-outcome
triggers:
  - task.completed
  - task.failed
scope: system
---

# Task Outcome

When a task completes or fails, evaluate it and take follow-up action.

If the task completed:
  First, review the output against the acceptance criteria. Note
  whether it passed or had issues.

  Then, check if the completed task modified files that have
  corresponding specs. If the code diverged from a spec, create
  a task to update the spec. Skip this if the reflection step
  already flagged quality problems — spec sync is pointless if
  the work needs to be redone.

  Update project memory with insights from both checks.

If the task failed:
  Check whether this is a recurring failure by looking at recent
  task history.

  If the error is transient (rate limit, timeout, network), retry
  the task.

  If it's a code issue, create a fix task. If post-action-reflection
  previously flagged this area as problematic, include that context
  in the fix task.
