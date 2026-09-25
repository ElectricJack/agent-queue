# Integration identity diagnostics

`integration.reused_task_identity` is a report-only doctor check for tasks
created before task naming reserved identifiers retained by integration history.
A deleted task can leave an origin and checkpoint that a later task with the
same ID accidentally inherits.

The check reports every unretired branch origin whose `created_at` is strictly
earlier than the matching live task's `created_at`. It includes completed tasks
and runs regardless of integration mode. Retired origins and origins without a
live task are outside this check. Equal timestamps are not findings.

Each finding includes the task ID, project, status, creation time and current
repository/branch; the origin ID, repository, branch, base, materialization state
and creation time; and any checkpoint's repository, branch and SHA. The result
contains the total origin and task counts and at most 50 findings, ordered by
project, task and origin ID. Findings produce `warn`; an unavailable database
produces `info`, and no findings produces `ok`.

The timestamp comparison is evidence of a suspected identity collision, not
authorization to rebind. Imports or clock corrections can also affect timestamps.
Owner-row creation time is not evidence: an ownership transfer preserves it even
when the owner ID changes. The check performs no Git operations and has no fix.

Rebinding remains an operator decision. Releasing a fence alone does not replace
an origin or checkpoint. A repair must prove the exact predecessor branch was
delivered or explicitly discarded, exclude live writers and dependent integration
history, preserve the old origin for audit, and account for an existing ref before
reserving the current task's fresh base. This check changes neither deletion
semantics nor claim admission.
