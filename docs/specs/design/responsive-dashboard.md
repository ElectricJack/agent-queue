# One responsive dashboard

Accepted task: grand-ember-55, 2026-10-25. This replaces the separate Focus
surface described in older mobile and Discord design records.

Every screen uses the main dashboard shell. Below 768px its rail is a menu
drawer and its secondary surfaces are sheets. Pages must fit a 390px viewport,
wrap long identifiers and prose, scroll vertically, and expose usable touch
controls. Wide code, tables and terminals may scroll inside their own region.

## Feature inventory before removing Focus

| Focus behavior | Main dashboard destination |
| --- | --- |
| Filtered task cards and task actions | Project Tasks & Knowledge, task detail |
| Live agent and pool sessions | Agents and project Sessions |
| Provider quotas and age | Header usage bars and Metrics |
| Report and task evidence | Report page and task detail |
| Needs-you, waiting and history, including pagination | `/inbox`, shared AskInbox also used by Reviews and Activity |
| Quick ask replies, decisions and receipts | `/asks/:id`, shared AskDetail and discussion/decision controls |
| Review/escalation audience-checked resolution | `/reviews/:id`, `/escalations/:id` resolving to the canonical ask |
| Batch delivery state, PR and member tasks | `/batches/:id` in the main shell |
| Conversation picker/history and quick replies | `/conversations?conversation=:id` using existing GlobalChat |
| Process-pinned terminal and restart notice | `/sessions/:id?started=:epoch`; explicit selection before following a restart |
| Ended session explanation and current-session link | Session detail, alongside its historical transcript |
| Cold-link navigation, safe areas and phone controls | Main shell Back/menu, dynamic viewport and safe-area utilities |

Posted links use these canonical routes at every width. All former `/focus`
routes redirect with replacement history, preserving query strings, encoded
identifiers and fragments. `/focus` and unknown Focus paths return to Command
Center; `/focus/inbox` becomes `/inbox`, and conversation detail becomes the
selected conversation query. There is no separate mobile mode or link policy.

Validation uses dashboard typecheck, build and Vitest, Python posted-link and
doctor tests, and the browser layout harness at 390px (including menu behavior,
task, ask, review, session and conversation screens).
