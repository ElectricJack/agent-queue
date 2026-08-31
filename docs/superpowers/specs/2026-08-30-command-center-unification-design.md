# Command Center task workspace

The sidebar is the single project selector for the main workspace. `/command-center/graph` and `/command-center/tasks` show all projects; `/projects/:projectId/graph` and `/projects/:projectId/tasks` use the same layout and controls for one project. Project selection retains the current compatible tab and filters. Project overview, sessions, workspaces, profiles, playbooks, and config remain accessible as tabs in that layout. Project Chat disappears; old chat links lead to the flock.

Graph and Tasks share title/ID/project/agent search, a status filter, a completed toggle, and an Add task action. `N` opens creation outside editable controls; `/` focuses search. Creation defaults to the selected project, reports errors, and refreshes graph and task data immediately. Existing table edit and action functionality remains available. A task card or table row opens its detail pane; blank workspace clicks clear task selection without closing unrelated panes.

The graph lays tasks out left to right in a bounded number of columns before wrapping down. Node ordering must not depend on changing task status. Dependencies point from prerequisite to dependent. Canonical `parent-child` edges identify child-to-parent relationships. Parent tasks start collapsed with child counts and completed progress; expanding reveals descendants. Filters retain necessary ancestors and searching a hidden child reveals its path. Collapsed dependency endpoints map to the nearest visible ancestor, omitting self-edges. New data must not reset user pan/zoom.

Live task creation, edits, hierarchy/dependency changes, status transitions, deletion, and agent assignments reconcile through existing WebSocket events and query invalidation. No database schema or runtime changes are planned. Empty and failed query states must be distinct.

## Safety and validation

Work in the existing isolated global-agent-flock worktree. Main has an unrelated in-progress backend merge; preserve it and all running tmux sessions. No pushes or test tasks in active production queues. Verify pure layout and hierarchy invariants, project navigation, shared filters, selection/deselection, quick-add keyboard behavior, and live invalidation. Use a separate dashboard preview against the existing read API for browser QA; use isolated fixtures for synthetic tasks. Preserve tracked TypeScript build-state bytes around verification and integration.
