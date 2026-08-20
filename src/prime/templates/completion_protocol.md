When you are done with this task, close it explicitly — do not just stop:

    aq task close {task_id} --outcome pass|fail [--failure-class transient|hard] \
        [--work-outcome shipped|no-op|blocked|abandoned] [--commit <sha>] [--notes "..."]
    aq session drain-ack

An explicit close is what lets the scheduler promote the next task. If you're blocked on a
human decision, use `aq task ask "<question>"` instead of stopping silently.

## Stay visible while you work

The daemon holds a lease on this task and watches for activity. If it sees nothing for
`sessions.lease_ttl_seconds` (default 8 minutes) it treats you as stalled — first a nudge,
then an interrupt, then a kill and a restart. That is deliberate: a genuinely hung agent has
to be recoverable without a human noticing.

So before anything that will run quiet for more than a few minutes — a full test suite, a
long build, a large install, a big download — refresh the lease first:

    aq task heartbeat {task_id}

It is one cheap call and it buys you another full lease window. Call it again in the middle
of anything really long. Silence is the only thing the daemon can read as trouble, so do not
make it guess.
