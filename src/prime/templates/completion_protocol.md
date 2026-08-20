When you are done with this task, close it explicitly — do not just stop:

    aq task close {task_id} --outcome pass|fail [--failure-class transient|hard] \
        [--work-outcome shipped|no-op|blocked|abandoned] [--commit <sha>] [--notes "..."]
    aq session drain-ack

An explicit close is what lets the scheduler promote the next task. If you're blocked on a
human decision, use `aq task ask "<question>"` instead of stopping silently.
