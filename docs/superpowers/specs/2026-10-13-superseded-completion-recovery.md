# Supersede a retained completion owned by a live Subject

Task: steady-crest-27. Scope: one unheld train root, one current passing
completion, one source or parent-episode Subject. This control grants no delivery
authority. It never invokes the completion pipeline or merges a PR.

Existing obsolete close refuses non-done Subjects. Quiesce only handles empty
root admission and changes project scheduling. Historical cancel-preserving is
not a registered control in this source revision. Neither resolves this case.

`aq integration supersede-completion` is an operator or live owning supervisor
control. Preview requires project/task, Subject id/version, claim epoch,
completion id, exact source head, and original verified escalation reply id.
The reply must belong to an existing escalation bound to this project/task.
The authorized caller interprets that reply as a supersession directive and
records the reason; agent reports and caller-authored human flags are not proof.
This does not create or resolve an escalation or apply any gate answer.

The preview returns a SHA256 over the full retained identity/proof. Apply
requires that digest and repeats the proof, including fresh remote immutable
completion retention and provider-backed stop checks. It takes nonblocking
exclusive repository and parent operation locks, the hierarchy lock and row
locks. Changed task/source/episode/operation/owner/session identities refuse.
Live or unknown writers, other active Subjects on the task or source branch,
open batches/children, active gates, unresolved writes, active delegates and
unreleased collector owners refuse with instructions to settle those through
their owning controls. The command does not stop processes or bypass holds.

In one transaction the exact parent operation, when present and free of
delegates, becomes cancelled (completed history stays completed), pending
unowned stages become cancelled, and the Subject becomes done with an obsolete
reason. Its append-only journal retains the original completion and source,
episode/operation and owner/writer proof, original human reply and executor.
Normal ObsoleteClose then records abandoned metadata and retirement, without
adding or replacing any completion. Managed parents accept only a transaction-local
settlement receipt for this abandoned obsolete transition; ordinary completion and
root authorization still require their existing authorities. Any obsolete-close refusal rolls back the
Subject and operation settlement. Original checkpoint, origins, gates, budgets,
passing completion and retained source remain intact. Normal owner preservation and
PR/branch cleanup run after commit; pending cleanup is reported truthfully.

Replay of the same digest/arguments returns the original settlement receipt and
retries obsolete cleanup on apply without duplicate audit or completion. Dry-run replay
does not retry cleanup. Changed replay
arguments, reopened tasks or changed versions refuse. Preview is read-only in
PostgreSQL. Remote reads may update the daemon's retained Git cache.

Verification uses disposable PostgreSQL and bare Git remotes. Loaded deployment
and another project's eligibility require runtime receipts from its owner;
local source/tests alone do not establish either.

Caller recipe and proof field definitions: [superseded-completion guide](../../guides/superseded-completion.md).
