# Tasks & Knowledge command-center view

Task: `fair-dune-76`. The project tab strip contains Graph and one Tasks &
Knowledge tab, followed by the existing project resource tabs. The canonical
URL is `/projects/:projectId/tasks-knowledge`. Legacy `tasks`, `knowledge` and
`records` routes replace their history entry with this route and keep query
parameters. Task and knowledge entry points default to their corresponding
kind only when no kind is supplied. Legacy knowledge selections gain their
kind discriminator; task-pane history selections become URL selections.

The combined view retains mixed record search and its graph. Task-only browsing
uses the existing task table, filters and controls. When knowledge activation is
unavailable, task browsing remains available without calling record search.
Knowledge permission and pinned-revision behavior remain server-controlled.

Selection uses `record` and `recordKind` for record identities, or `task` for a
task id that does not require a knowledge record lookup. It changes independently
of filters and pushes browser history. Close removes only selection parameters;
Back and Forward restore selections. The list stays mounted with its own scroll
container. Selected rows have a visible highlight and accessible selected state.

The right detail surface reuses TaskDetailBody and KnowledgePane. It has Close
and Open full page controls. Desktop width can be changed by dragging its
separator or using Left/Right on the separator. Below 1024 CSS pixels it is a
full-screen modal sheet with a Back button, contained focus, and an inert list.
Escape closes detail unless a nested dialog owns it. Up/Down and J/K on list
rows move focus and selection without interfering with editable controls or
detail tabs. Closing restores focus to the selected row without scrolling it.

Project navigation, palette task search, and knowledge citation links generate
the canonical route and selection. Existing task full pages and mobile focus
routes remain available as explicit detail destinations, including Discord's
focus deep links. Verification covers tab lists, redirects, query preservation,
selection/reload/history/close, keyboard navigation, resizing, and narrow layout.
Before and after browser screenshots use a deterministic stub daemon and are
included in the PR.
