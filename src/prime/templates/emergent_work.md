## Emergent work

File confirmed out-of-scope findings with
`aq task create --project "$AQ_PROJECT_ID" --title "..." --description "..." --reason "..."`.
State the originating task/evidence and why in both reason and description. Give --type
and optionally --intelligence-class; never pin a profile/provider/model or bypass routing.
Do not invent speculative epics or list other workers' tasks to deduplicate.

A filing is a child of the task you hold, with a discovered-from edge.
An open child blocks passing close (hierarchy.open_children): resolve it or ask the
supervisor; never abandon, re-file or move required work merely to make your close pass.
For a genuinely misplaced unclaimed filing use
`aq task reparent --task-id <finding-id> --parent-id <container-id>` (or --root).
Authorized alternatives for --parent <id> are the held task's immediate parent or its
descendants. --root is for cross-cutting work and receives a routing gate;
`--parent` and `--root` cannot be combined. Never gate a filing on the held task or an
ancestor (dependency_on_ancestor); order new tasks among themselves.
An explicitly requested epic needs a graph with children, not a claimable bare task.
Worker tokens cannot create graphs; request supervisor routing. See the aq-tasks skill.
