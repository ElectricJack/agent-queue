# Explicit re-routing recovery

This amends the explicit re-run behavior in the mandatory-task-routing design
§6.1 and §7. A successful `aq task route` must request routing immediately,
including when the task was already unrouted. Clearing the cascade's throttle
alone is insufficient: its candidate scan excludes some READY tasks, including
blocked rows without an open routing gate. Those tasks can otherwise remain
unrouted indefinitely after a successful re-run.

After the existing authorization, writer checks and route reset, emit
`task.route_needed` for the refreshed task and its project's bound router.
Use the same payload as periodic routing, including the updated class hint,
kind and task identity. Every explicit re-run bypasses the periodic throttle
and candidate scan; it still leaves route selection to the bound playbook.
Writer safety and the accepted task states remain enforced by the command.

Record the throttle timestamp after a successful emission. The next periodic
scan must not duplicate that request within 120 seconds. A failed emission
must not set the timestamp or report the command as successful; a later
explicit re-run or eligible periodic scan can retry.

The read-only operator stall sweep reports `UNROUTED-READY` for READY tasks
older than its existing 20-minute threshold whose list observation says
`route_source=unrouted` and whose canonical explanation confirms a routing
wait or failure. Preserve that finding even when another explanation describes
a moving dependency. Canonical routing explanations exclude containers. Fresh,
routed and non-READY tasks do not receive this diagnostic. Existing scope,
authorization and incomplete-evidence handling still apply; the sweep never
re-routes work itself.

Regression coverage lives in `tests/test_task_route.py` and
`tests/test_supervisor_stall_sweep_script.py`.
