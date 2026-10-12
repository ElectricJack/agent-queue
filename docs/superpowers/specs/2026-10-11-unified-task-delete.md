# Unified task deletion

Task: `prime-crest-99`. Supersedes the delete/remove behavior in the September
task deletion and integration history designs. Archive keeps its existing contract.

`delete_task` is the single operator/live supervisor action. `remove_task` and
`aq task remove` are deprecated aliases with identical options and behavior.
Authorization, malformed input, and missing task errors still apply; task state
does not prevent a confirmed delete.

An unconfirmed request is a read-only preview. It lists selected task identities,
surviving children, sessions, dependencies, gates, reviews, escalations,
integration batches/operations/owners, and attributable branches and worktrees.
The CLI and dashboard choose recursive scope and keep/delete branches before
showing that preview and requesting one final confirmation. Reasons are optional.
The scripting flags are `--recursive`, `--branches keep|delete`, `--stop-running`,
`--reason`, and `--yes`; `--cascade` remains a compatibility spelling.

Deleting only a container keeps its children and reparents them to its surviving
parent (or the project root). Immutable task ids do not change. Recursive scope
selects the entire subtree. Selected running work always stops, even without
`--stop-running`: the preview explicitly describes this mandatory consequence.
The flag acknowledges it; it cannot leave a writer running against a deleted task.

Confirmed deletion first fences the selected tasks and sessions, cancels their
integration authority and writes an audit intent containing complete task snapshots,
the actor and choices. Provider-backed session stop revokes credentials and sweeps
marked subprocesses. Cleanup errors become result warnings; no state guard refuses
the delete. The task transaction releases claims/workspace locks/branch owners,
drops dependency edges, withdraws owned reviews and decisions, aborts affected
batches, retires branch origins, and deletes task identities while retaining audit
and integration history. Frozen batch manifests remain historical; cancellation
removes their authority to publish the deleted work.

Branch discovery combines task branch names, origins (including old origins),
checkpoints, owner refs, selected worktree checkouts and task recovery refs. It
examines matching repository clones and retained stores. Keep lists everything
retained. Delete records local and remote tips before removing exact refs and
associated worktrees, including local-only branches. Default repository branches
are excluded as repository infrastructure, with a warning if task metadata names
one. Remote failures and moved refs are reported individually, never disguised as
successful cleanup. The final audit records every cleanup result and warning.

The task explicitly adds one Discord operator action, `/delete-task`, superseding
notification-only policy for this action alone. It requires an allowlisted operator
or administrator in the configured AQ guild/channel and binds the view to that
requesting user. Scope and branch selectors refresh the shared preview; a single
confirmation calls `delete_task`. An optional slash-command reason is audited,
and long previews are attached in full. The six retired slash commands stay retired.
The verified Discord actor is carried in server-only context, never request args.

Validation covers read-only preview, alias parity, recursive/single scope,
running and failed-stop paths, gates/reviews/escalations/dependents,
sealed/live/delivered/history/retained-owner conditions as warnings, optional
reason and surviving audit, and real Git local/remote/worktree cleanup. Run
focused and area checks, regenerate command/API/client/catalogue artifacts,
and run the disposable swarm smoke environment after hierarchy/claim changes.
