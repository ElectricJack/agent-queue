# Command Center Unification Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement the independently owned slices below.

**Goal:** One project-scoped Command Center with an interactive, compact, live task graph and shared task controls.

**Architecture:** Reuse CommandCenter for global and existing project routes. Shared TaskWorkspace context owns URL-backed filters and selected project IDs. Graph hierarchy is projected from existing typed edges before a bounded row layout.

**Tech Stack:** React 19, TypeScript, React Router 7, TanStack Query 5, React Flow 12, Vitest. No additional dependency.

**Spec:** docs/superpowers/specs/2026-08-30-command-center-unification-design.md

## Global constraints

- Isolated worktree: /home/jkern/dev/agent-queue2/.worktrees/global-agent-flock.
- Preserve unrelated main merge, build-state bytes, and all tmux sessions.
- No new backend schema or agent runtime changes; extend existing task event emitters only where needed. No pushes or synthetic active production tasks.

## Task 1: Project navigation and unified shell

Files: App.tsx, pages/CommandCenter.tsx, shell/LeftRail.tsx, pages/project/ProjectLayout.tsx, pages/project/Overview.tsx, navigation helper and route tests. Consume TaskWorkspaceProvider and TaskToolbar from command-center.

- [x] Cover selected project scope, compatible tab/filter retention, legacy redirects, and absence of Chat in route tests.
- [x] Mount shared Graph/Tasks and project resource pages in one layout; sidebar selects project or all.
- [x] Run focused navigation tests and review deep links.

## Task 2: Compact task graph and hierarchy

Files: command-center/{GraphCanvas,TaskNode,MobileCardList,AgentAvatarLayer}.tsx, layout.ts, types.ts, hierarchy.ts, graph tests. GraphCanvas consumes graph, onTaskClick, selectedTaskId, onBackgroundClick, matchingTaskIds, and filtering.

- [x] Test bounded width/no overlap, stable status updates, canonical dependency direction, nested collapse, ancestor preservation, collapsed-edge remapping, and filtered descendants.
- [x] Implement hierarchy projection and bounded rows; full-card interactions, blank-canvas deselection, expansion controls, progress summaries, explicit zoom controls.
- [x] Run focused graph tests and inspect representative fixture layout.

## Task 3: Shared task controls and rich task list

Files: command-center/{TaskWorkspace,TaskToolbar,Graph,Tasks}.tsx, taskFilters.ts, task action components, components/CreateTaskModal.tsx, focused interaction tests.

- [x] Test search/status/completion matching, shared URL filter state, scoped add defaults, errors, row selection, and protected interactive controls.
- [x] Add shared toolbar and creation shortcut; remove duplicate selectors. Preserve rich table edit/stop/restart/approve/delete actions and pane navigation.
- [x] Run focused interaction tests.

## Task 4: Live updates

Files: command-center/useGraphLive.ts, api/graph.ts, api/hooks.ts, existing graph API tests and additional live tests.

- [x] Reproduce missing creation/edit/hierarchy/delete updates with event fixtures.
- [x] Add precise query invalidation and reconciliation for existing WebSocket events; ensure mutations refresh graph.
- [x] Run focused event and query tests.

## Task 5: Integration and live verification

- [x] Review all slices together; run focused tests, edited-file lint, typecheck, and production build.
- [x] Browser-test sidebar scope, all/project tabs, graph selection/blank deselection, filters, hierarchy, and Add task with Cancel. Do not create real tasks or interrupt sessions.
- [x] Integrate the verified feature with current main, preserving unrelated changes. Verify dashboard and API health and report actual limitations.

Validation notes: direct browser checks used existing task reads and an isolated local graph fixture; no real tasks were created. Main’s unrelated merge completed before integration. Full-suite test isolation required explicit shared DOM cleanup and a scoped API-mock reset; application typecheck and production build passed.

Final verification: merged backend checks passed (533 passed, 1 skipped); frontend suite passed 521/524 with the same three existing shortcut/palette failures. Strict TypeScript and production builds passed in the integrated worktree and main. AQ was restarted from main with `--keep-sessions`; `/health` is healthy, all four agents remain idle/stopped, and no test tasks were created. Browser checks on the live main dashboard confirmed full-card selection, blank-click deselection, shared sidebar scope, preserved filters, and scoped creation defaults. Existing main edits were preserved.
