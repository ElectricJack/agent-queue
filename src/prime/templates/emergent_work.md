## Emergent work

When you discover work while executing the current task that is outside its scope — for
example, a bug, missing documentation, follow-up, or spec divergence — file it instead of
silently expanding your own scope. Then keep moving on the task you hold.

Before filing, deduplicate with `aq task list` and any available dedup keys. File one task
per distinct, confirmed finding; do not create speculative epics.

Use `aq task create --title "..." --description "..." --reason "..."` with a clear title
and description grounded in what you found. The worker filing path creates the
`discovered-from` edge back to the task you hold; make `--reason` say why the task exists,
referencing the current task and the finding. If the edge-reason field is unavailable, put
that why in the first line of the new task's description. If you need to add an edge
explicitly, use `aq task add-dependency` with `--dep-type discovered-from` and the reason.

If the current task is a child of a container or epic (`parent_task_id` is set), generally
create the emergent task under that same parent with `--parent <container-id>` so it remains
grouped with the epic. File review/exit-gate work and other cross-cutting work that does not
belong to the epic's deliverable at project level instead.
