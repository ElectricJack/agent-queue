## Pool session — keep pulling work

This is a `lifecycle: pool` session: nothing gets pushed to you. Chain the
next claim straight onto the close instead of stopping:

    aq task close {task_id} --outcome pass|fail --summary "..." \
        --claim-next --wait 60

`--claim-next` claims the next ready task matching this session's profile
right after the close lands; `--wait N` long-polls for up to N seconds
before giving up with `no_ready_work`. If you ever need to claim without
closing first (e.g. this is your first task), use `aq task claim --next
--wait 60` directly. Every claim rewrites `<work_dir>/.aq/claim.json`
with the new `claim_epoch` — `aq task heartbeat` / `aq task set` / `aq
handoff` all read it from there automatically, so pass `--claim-epoch`
explicitly only if you need to override it.
