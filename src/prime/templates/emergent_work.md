## Emergent work

When you discover work while executing the current task that is outside its scope — for
example, a bug, missing documentation, follow-up, or spec divergence — file it instead of
silently expanding your own scope. Then keep moving on the task you hold.

File one task per distinct, confirmed finding; do not create speculative epics. Your
session token cannot read the project's queue (`list_tasks` is off the agent surface), so
do not try to deduplicate by listing — a worker-filed task lands DEFINED with a routing
gate for triage, which is where dedup and routing happen. Write the title so that
judgement is easy: name the symptom and the file, not a generic area.

Use `aq task create --project "$AQ_PROJECT_ID" --title "..." --description "..."
--reason "..."` with a clear title and description grounded in what you found. Pass
`--project` explicitly: without it the CLI first asks the daemon to list projects, which
your token refuses. The worker filing path creates the `discovered-from` edge back to the
task you hold; make `--reason` say why the task exists, referencing the current task and
the finding. Repeat the same why in the first line of the new task's description, so it
survives for readers who only see the task.

Say where the task belongs; do not leave it to be inferred. If the current task is a child
of a container or epic (`parent_task_id` is set), generally create the emergent task under
that same parent with `--parent <container-id>` so it remains grouped with the epic. File
review/exit-gate work and other cross-cutting work that does not belong to the epic's
deliverable at project level with `--root` instead. `--parent` and `--root` are mutually
exclusive — passing both is refused — and passing neither reads to a reviewer as a
forgotten `--parent`, so pick one:

```
aq task create --project "$AQ_PROJECT_ID" --root --title "..." --description "..." --reason "..."
```

A `--root` filing lands DEFINED with a `discovered-from` edge back to the task you hold and
a routing gate for triage, exactly as an unplaced filing does; the flag records that project
level was the intent.
