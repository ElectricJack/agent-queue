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

## Held verifiers with trusted red CI

A completed additional fix may also reopen collection while the old verifier
still holds its task. Require a unique immutable verifier handoff matching the
current episode, generation and head, plus conclusive failed CI from the frozen
parent check producer and version, naming a failed required check. The CI must
postdate that handoff. Stale generations, infrastructure failures and untrusted
producers cannot authorize settlement.

Dry run reports the evidence and required handoff without stopping or writing.
Apply repeats all facts under the project lock before asking the existing fenced
provider stop/detach protocol to release an attached verifier. Missing stop proof
or an unpublished/dirty checkout remains a refusal; never discard its work.
Repeat diagnosis and remote proof after handoff. Only a detached verifier with
no remaining session/claim/workspace hold can be settled FAILED in the reopen
transaction. Append a completion naming the immutable head and CI evidence;
keep all historical completions, CI evidence, receipts and consumed budgets.
The recovery audit retains the original attachment as well as the final fence.
The parent observer treats exact trusted red as verifier failure so the existing
policy can collect the new fix; existing manual holds and human gates still bind.

## Durably refused reopens reach a human gate

A reopen refusal no retry can satisfy — an ambiguous verifier subject, a manual
hold, a dirty checkout — is not an unknown answer forever. `blocked` and
`ambiguous` are durable; `changed`, `not_eligible` and every other refusal still
retries, because re-diagnosing can still succeed and a false human gate is worse
than a retry. The adapter records a durable refusal as `integration_reopen_refused`
metadata naming the episode, operation, head and generation, with its refusal and
reason, and the merge primitive still answers `unknown` — it cannot merge. The
observer reports `reopen_refused` only for that exact identity, so a later
generation or a malformed marker never holds a parent, and the pinned policy's
`blocked-reopen` case routes it, together with a failed verifier, to the existing
no-default `red` gate: the same human gate a held red verifier reached before
trusted red counted as a failure. A successful reopen clears the marker, and the
refusal never settles, skips or discards anything.

## Legacy failures with an empty commit list

An otherwise settled failed completion with exactly `commits=[]` may use the
immutable `task.integration_ready` outbox subject instead. Require exactly one
event naming that verifier, matching project, parent, operation, episode,
repository, branch, head and verifier handoff identity. Its original generation
must lie between the episode generation and the current checkpoint generation,
and the event must follow episode creation and predate the failed completion.
A later checkpoint generation does not replace the original verification
generation. Missing, conflicting or
malformed evidence, or a nonempty mismatched completion, is refused. Summary
prose and mutable task metadata never supply the missing subject.

The dry run reports this subject and failure completion id without writing.
Apply repeats the frozen evidence and settled-writer checks under locks, proves
the exact remote tip again, and includes the complete binding and outbox id in
the append-only recovery event. The historical completion stays byte-for-byte
unchanged. Future aggregate verifier failure closes capture this same frozen
subject head before the terminal transition, including in the pending completion
draft used after a crash; missing or contradictory subjects refuse the close.

For calm-grove-25, the operator must first inspect session
`eb381939-9c3e-4b65-88d1-456373d1271d`. If it still runs, stop it through
`aq session kill SESSION_ID` and let reconciliation confirm the process stopped.
Use `aq integration release-owner --task-id VERIFIER_TASK_ID --dry-run`, inspect
its preservation/stop proof, and then run the same command without `--dry-run`.
A live writer or ambiguous checkout remains a refusal. Finally dry-run
`aq integration reopen-collection calm-grove-25`, require the recorded remote
head `7ce43c68680dda4c4b62c53820c8d792ffe3d685`, and apply with that head and an
audit reason. Collect calm-grove-25.6 through the ordinary collector and require
its receipt and a fresh verifier at the resulting head. If immutable subject
evidence is missing, stop and report it; do not edit the completion or database.
