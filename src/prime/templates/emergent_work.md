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

By default the new task is a **child of the task you hold**, so it stays visible with the
work that exposed it. Pass `--parent <id>` only to choose an authorized alternative parent
(your task's immediate parent, or a descendant of your task); nothing further up or across
the tree can be selected. Pass `--root` when review, exit-gate, or other cross-cutting work
does not belong to this deliverable. The root filing keeps its `discovered-from` edge to the
task you hold and receives a routing gate; `--parent` and `--root` cannot be combined.

An open child blocks your successful close (`hierarchy.open_children`) — that is intended.
Resolve it, or record it on your task and ask the supervisor how to proceed; do not re-file,
abandon, or move it aside merely to make your close pass. If a filing was simply misplaced,
move it with `aq task reparent --task-id <finding-id> --parent-id <container-id>` (or
`--root`). You may move only unclaimed tasks you filed, to the same parents you could have
filed under; a move to root receives the routing gate a root filing gets.
