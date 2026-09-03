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

If the current task is a child of a container or epic (`parent_task_id` is set), the new
task is placed as your sibling under that same parent by default, so it stays grouped with
the epic; `--parent <container-id>` naming that same parent is accepted but not required.
Pass `--parent <your-task-id>` only when the work belongs *under* your own task. Nothing
further up or across the tree can be selected as a parent. Pass `--root` when review,
exit-gate, or other cross-cutting work does not belong to the epic's deliverable. The root
filing keeps its `discovered-from` edge to the task you hold and receives a routing gate;
`--parent` and `--root` cannot be combined.
