## Pool session — close and follow the next-claim result

This is a `lifecycle: pool` session: nothing gets pushed to you. Close your
task and inspect the next-claim result:

    aq task close {task_id} --outcome pass|fail --summary "..." \
        --claim-next --wait 60

The task id is optional — omit it and the daemon closes whatever task this
session currently holds, which is what the loop below does.

By default, AQ retires this conversation after one task so the next task starts
with fresh context on the same global worker. On `drain_requested` or
`session_exhausted`, stop working and exit; do not claim another task or send
`/clear` yourself. This does not clear context while your current task is active.

If the operator explicitly disables `swarm.fresh_context_per_task`,
`--claim-next` may claim the next ready task matching this session's profile
right after the close lands; `--wait N` long-polls for up to N seconds
before giving up with `no_ready_work`. If you ever need to claim without
closing first (e.g. this is your first task), use `aq task claim --next
--wait 60` directly. Every claim rewrites `<work_dir>/.aq/claim.json`
with the new `claim_epoch` — `aq task heartbeat` / `aq task set` / `aq
handoff` all read it from there automatically, so pass `--claim-epoch`
explicitly only if you need to override it.
