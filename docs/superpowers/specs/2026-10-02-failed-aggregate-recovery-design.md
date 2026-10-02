# Failed aggregate verification recovery

`aq integration reopen-collection PARENT` also diagnoses an aggregate verifier
that closed with a failure. Apply requires the exact remote parent head reported
by the dry run and an audit reason. This is an operator/supervisor control;
worker scope and human rollout gates are unchanged.

Recovery keeps the same collection operation and episode, including every
receipt, frozen policy, repair stage, attempt, deadline and failed completion.
It requires an unassigned PAUSED parent, a settled failed verifier with a failed
completion for the exact checkpoint head, no attached workspace or live session,
no unsettled repair stage, no ambiguous external mutation, no manual/terminal
hold, and a detached released or reserved fence belonging to that operation or
verifier. Git must prove the remote head equals the checkpoint and contains the
episode base and receipt heads. A completed child with a new, unreceipted head
must exist; recovery cannot retry verification of the same red aggregate alone.
The verifier may be FAILED or BLOCKED by its hard-failure/exhausted-retry close.
Those completion markers and consumed retry counts stay intact; a different
terminal reason or a manual pause is refused.

Under the project and row locks, repeat all facts and remote proof, transfer the
collector reservation at the next fence token, advance checkpoint generation and
version, clear verification pointers, and return to awaiting_children. Clear the
operation's verifier binding, preserving the old task and completion in an audit
event along with receipt ids, stage budgets, ownership and the old exact head.
The ordinary collector then delivers the additional child. A fresh verifier id
binds the resulting generation and head; never reuse a failed verifier task.
Old readiness events carry the obsolete fence, and old check evidence carries
the obsolete generation. Neither can certify the recovered aggregate.

Focused tests drive failed verification, reopening, additional child delivery,
fresh verifier handoff and refusal of moved heads, attached writers, ambiguous
mutations, missing failure evidence, missing fixes and manual holds. Live
deployment, recovery and Phase 1 verification belong to the operator because
pool workers cannot restart the daemon or mutate tasks they do not hold.
