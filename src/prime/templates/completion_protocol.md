When you are done with this task, close it explicitly — do not just stop:

    aq task close {task_id} --outcome pass|fail [--failure-class transient|hard] \
        --summary "What changed, findings, verification, and remaining issues"
    aq session drain-ack

## Deliverable self-check

If this task lists **Deliverables**, re-read the plan section you implemented and reconcile
each listed file, test, command, flag, or registration before you close. A passing close
checks files and symbols in the worktree; declared test deliverables must also be named in a
recorded repeatable `--test "..."` command. If an item is intentionally not shipped, make it
visible to the reviewer with one `--deliverable-unmet 'id: reason'` option per item. A pass
with an unlisted gap is refused and keeps the task claimed so you can correct it.

An explicit close is what lets the scheduler promote the next task. If you're blocked on a human decision, report it with aq message send --to user:dashboard --project "$AQ_PROJECT_ID" --body "Blocked: <question>" instead of stopping silently. The canonical human-operator recipient is `user:dashboard`.

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


## Save findings before closing or handing off

Keep the task description current with confirmed findings that change how this task
should be completed. Preserve the original goal, requirements, and acceptance criteria;
do not replace them with a progress log. Read the current description first, then write
the complete updated text using `aq task set {task_id} --description "..."
--expected-description "<description you read>"`. If it conflicts, re-read and merge;
do not force an overwrite of someone else's findings.

Append meaningful progress, evidence, decisions, test results, and blockers as comments:

    aq task comment {task_id} --body "Finding: ... Evidence: ... Next: ..."
    aq task comments {task_id}

Comments are durable, attributed history for the next agent. Record useful findings as
you discover them and before close, handoff, or waiting for input; terminal output and a
close summary are not substitutes. Distinguish confirmed facts from hypotheses. A comment
does not request approval or notify the user: use the question/message workflow for input.
Do not put secrets into descriptions or comments. New sessions get recent comments in prime;
read full history when necessary. No findings means no invented update is needed.
