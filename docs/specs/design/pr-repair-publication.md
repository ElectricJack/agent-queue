# Publish a separate repair to an existing root PR

A separately filed repair keeps its own branch and publishes there through the
ordinary worker guard. A local operator or live project/global supervisor can
then run `aq integration fast-forward-pr --task-id REPAIR --pr NUMBER` to adopt
the repair's published passing completion into the original PR branch. This is
a CommandHandler operation, independent of the repair worker's session or claim.
The default dry run reports the selected heads without acquiring publication
authority; `--apply --head PREVIOUS_OID --reason TEXT` publishes exactly the head
the dry run reported.

The server derives the project and designated repository from the repair task,
reads the authenticated PR, and requires an open, same-repository PR tracked by
a completed root. Default and promotion branches are forbidden. The repair's
latest passing completion must name its current published branch head in full.
The PR head must be an ancestor of that exact immutable commit. An already equal
head is an idempotent success. Unfinished tasks, attached writers, unavailable
observations, missing refs, changed identities and non-descendants refuse.

Publication acquires the PR branch's finite managed lease itself; the caller
does not supply an owner fence. An existing live owner is never displaced.
Under the project/task locks and branch exclusion it rechecks the identities,
PR state and remote head, then performs a validated push with the observed head
as an exact remote lease. Reserved paths and repair author identity checks still
apply. The original PR history and the authenticated PR base's published history
are inherited for identity validation. Because GitHub's PR base SHA can lag the
base branch, its current authenticated remote tip is also inherited. Preview and
apply read that tip with `als_remote_ref` against the designated repository; no
local branch or remote-tracking ref supplies this authority. The base branch and
exact PR base SHA are rechecked under branch exclusion, where apply observes the
current remote base tip. Both base OIDs must resolve to commits in the retained
store. A changed PR base identity or unavailable remote base evidence refuses
publication; commits unique to the repair still require the project's committer
identity. Reserved-path validation continues to compare the old PR head with the
repair tip.
The audit records the repair, completion, PR, previous and published heads,
base branch, PR base SHA and observed `remote_base_oid`, operator and reason. No
receipt, check approval or task completion is fabricated; ordinary exact-head
checks and train admission continue to apply to the repaired source.

The CLI gives `integration_fast_forward_pr` and `integration_redrive_root` a
300-second read timeout so authenticated fetches and remote proofs can finish
before the client reports an unknown outcome. Other commands retain their
existing budgets.

Automatic source-CI recovery reopens the original task once. On exhaustion its
supervisor notice points to this control for a separately filed repair; it does
not create another repair chain or widen a worker's branch scope.
