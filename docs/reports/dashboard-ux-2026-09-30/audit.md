# Command Center UX audit — 30 September 2026

Task: `quick-flare-50`. Source baseline: `bd35e46f0`. Actual harness: `gpt-6-astra`.

## Before evidence and bounded design

Inspected the running dashboard at 1440×900 and 390×844. [Desktop before](live-before-desktop.png) and [phone before](live-before-phone.png) are actual browser captures. Requests that could mutate state were blocked; the resulting “Preference not saved” notice is an inspection artifact, not an observed production fault. No production settings were changed. The baseline Tasks browser checks also passed both viewports against the repository's isolated fixtures.

| Finding | Evidence | Selected improvement |
|---|---|---|
| Controls dominate a phone's task view | At 390×844 the list starts at y=360, leaving 484 px. Search, time, provider and layout controls occupy four rows. | Keep search, status and creation prominent; disclose secondary filters. Keep active hidden filters visible in the disclosure label. |
| Project identity loses space to metadata | “agent-queue” wraps into two lines beside the long repository URL. | Put repository metadata below identity; preserve pause/delete controls with touch targets. |
| Operators must inspect individual rows to assess trouble | The desktop capture contains three failed and two in-progress tasks, but the only summary is “5 tasks”. | Add labeled, counted status shortcuts for in-progress, blocked, waiting-input and failed work, using the same task data and filters. |
| Loading/error and empty results look like an empty queue | Source renders “0 tasks” before loading and generic unfiltered empty copy. | Distinguish unavailable/loading data; provide an explicit filter reset for empty results. |
| Status text differs between table and cards | Desktop uses raw `IN_PROGRESS`; cards display `IN PROGRESS`. | Use readable status labels for the table's existing override control without changing command values. |

Retain the existing dark palette, indigo navigation, semantic status colors, table/card breakpoint, virtualized rows, keyboard shortcuts, task detail panes and operational actions. Improve hierarchy with grouping and spacing rather than introducing decorative imagery. No relevant frontend design skill was installed in the available Codex skill catalogue; native React/Tailwind and the repository browser harness suit this work. No raster asset is needed.

## Product boundary and remaining directions

Read the proposed `docs/superpowers/specs/2026-09-30-project-input-requests-design.md` from the explicitly referenced supervisor checkout (not present in this branch). It calls for durable requests with separate received/checking/applying/completed states. Existing `WAITING_INPUT` tasks are **not** those requests, nor a complete count of human decisions. This change must label them as task status only and retain existing Reviews/Activity navigation. Structured forms, approvals, escalation adapters and backend continuation behavior remain outside this visual slice.

Requested file-ownership coordination before editing (messages `msg-0ecc07cd43c643edb9e338f1ebfae7f0`, `msg-da5bbb7c28704c1db5fc641d7afc5c1d`). Those used the documented legacy supervisor recipient; resent to the actual live supervisor session as `msg-9633555a1cc740de90157f76d737fa7d`. No reply received at audit completion. The owned worktree was clean at start; no other task/worktree was modified.

Filed follow-up `sound-journey-23`: project Overview derives completed/total percentages from the default active-only task-list read and defaults to zeros while unavailable; opting into completed history alone still encounters the list cap. A complete project-health summary needs authoritative data and explicit unknown states. Global Activity and Reviews are separate from the proposed project input-request view; do not relabel their counts as project input requests. This bounded change addresses task triage only.

## After and verification

The same captured real daemon read responses were replayed against the baseline and final built app on an isolated local server. These are browser renders of the actual application with a frozen real-data snapshot, not fabricated live status. The captured responses remain uncommitted because they contain unrelated project/session data. Browser fixtures used for interaction tests are separate and explicitly synthetic.

| View | Before | After | Result |
|---|---|---|---|
| Desktop 1440×900 | [Before](snapshot-before-desktop.png) | [After](snapshot-after-desktop.png) | Grouped controls, four direct status filters; the open secondary controls cost 53 px, and can be collapsed. |
| Phone 390×844 | [Before](snapshot-before-phone.png) | [After](snapshot-after-phone.png) | Scrollable task region grows from 484 to 548 px (+64, 13%); project title height drops from 56 to 28 px. |

The status shortcuts live inside the scrolling list: they add an at-a-glance summary rather than taking permanent space from the list viewport. Counts ignore only the selected status and retain search, time and provider scope. They describe loaded tasks, not global health, human gates or future structured requests. Loading/error hides counts; a filtered empty result offers a reset. Existing row actions, creation, `/` and `N`, URL filters, pane navigation and virtualized list behavior remain covered.

Verification (2026-09-30):

- Focused component run: 56 passed across TaskToolbar, Tasks and ProjectLayout, including status-filter scope/toggle, empty-result recovery, unavailable counts, mobile disclosure and keyboard activation.
- `npm -w dashboard run test -- src/pages/command-center src/pages/focus src/pages/project/__tests__/ProjectLayout.test.tsx src/App.navigation.test.tsx`: **346 passed**, 32 files.
- `npm -w dashboard run build`: passed TypeScript compilation and production build.
- Changed-file ESLint: passed for the five production TypeScript files and two changed component test files.
- `npm -w dashboard run check:layout -- --only tasks-tab,compact-shell,desktop-baseline --out ../.aq/ux-audit/final-layout`: shell checks **6/6 passed**. Four added Tasks assertions initially raced React after URL updates ([recorded run](browser-shell-and-tasks.json)); the check now waits for the rendered pressed state.
- `npm -w dashboard run check:layout -- --only tasks-tab --out ../.aq/ux-audit/tasks-final`: **5/5 passed** after that timing correction ([recorded run](browser-tasks-final.json)). Covers 320×568, 390×844, 844×390, 1440×900 and 200% zoom/reduced motion; overflow, touch targets, keyboard status/filter controls, pane Back/scroll restoration, no compact preference writes and no terminal attachment.
- `git diff --check`: passed. No Python/backend behavior changed.

## Wider inspection and limitations

Also inspected the actual [desktop Graph](live-graph-desktop.png), [phone Graph](live-graph-phone.png), [desktop Overview](live-overview-desktop.png) and [phone Overview](live-overview-phone.png). The graph's phone card fallback avoids sideways canvas interaction, but its sparse fixed-height cards limit density; graph layout/zoom behavior is deferred. Overview's repository/configuration cards read clearly, but operational trouble sits below infrastructure details on a phone, and its completion figure needs the separately filed data fix. The main navigation and Reviews count remain available, while Activity sits behind a bell; this slice does not resolve project-level input-request discovery.

The existing dark palette and status text supply useful visual consistency; secondary metadata remains small and low contrast in untouched rail/graph surfaces. New shortcuts use text, counts and pressed states as well as color. This was desktop Chromium and emulated narrow-viewport/zoom testing, not physical-device or screen-reader certification. Production was inspected read-only and was not rebuilt, reconfigured or deployed by this task.

