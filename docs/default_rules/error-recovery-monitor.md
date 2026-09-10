# Error Recovery Monitor

<!-- aq:historical -->
> **Historical sample.** Illustrative content that nothing installs or loads. It
> was written for an earlier runtime, so copying it will not reproduce the
> behaviour it describes. Start at [the documentation home](../README.md); see
> [historical material](../history/README.md).

## Intent
Detect failed tasks and hooks, then take corrective action or notify
the operator with actionable context.

## Trigger
When a task fails.

## Logic
1. Review the failed task's error message and output
2. Check if this is a recurring failure (same error in recent task history)
3. If the error is transient (rate limit, network timeout), retry the task
4. If the error is a code issue, create a follow-up task to fix it
5. Post a summary of the failure and any corrective action taken
