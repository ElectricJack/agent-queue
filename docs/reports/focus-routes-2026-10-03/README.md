# Focus routes for reviews, escalations, inbox, batches and conversations

Task `crisp-horizon-90.4` (child of epic `crisp-horizon-90`), implementing item 6 of
§9 of the approved spec *Discord as a chat extension of the supervisor*
(`projects/agent-queue/specs/2026-10-03-discord-as-a-chat-extension-of-the-supervisor.md`,
review `rev-brisk-flare`, approved 2026-10-03). Spec §5.4 (escalation page) and §6.1
(the URL scheme table).

## What shipped

| Route | Page | Reads |
|---|---|---|
| `/focus/reviews/:reviewId` | `FocusReview.tsx` | the shared `ReviewPane` reader, so the decision bar works on a phone |
| `/focus/escalations/:escalationId` | `FocusEscalation.tsx` | `escalation_get` / `escalation_reply`; decision, one-line context, task link, choice buttons, reply box, collapsed terminal form |
| `/focus/inbox` | `FocusInbox.tsx` | `escalation_list` + `review_list`; the open escalations asking for a decision and the reviews whose decider is the human |
| `/focus/batches/:batchId` | `FocusBatch.tsx` | the project's flat node list, matched on the epic delivery ref that names the batch |
| `/focus/conversations`, `/focus/conversations/:conversationId` | `FocusConversationList.tsx`, `FocusConversation.tsx` | `supervisor_inbox_status` / `supervisor_inbox_history`, then the shared `ChatConversation` transcript on the global supervisor session |

`routes.ts` grows one helper per object (`focusReviewHref`, `focusEscalationHref`,
`focusInboxHref`, `focusBatchHref`, `focusConversationHref`), each `encodeURIComponent`-ing
the id so a dotted or slashed aq id cannot change the route. `/focus` gains a two-link
row (Needs you, Conversations) so the routes are reachable from the phone home without a
new query.

Every read goes through the generated client or an existing hook — no `fetch`, no
dashboard-side business logic, and no new localStorage key.

## Two deliberate decisions

**Resolve is not a control on the escalation page.** `escalation_update` refuses a local
or service principal ("the owning supervisor must execute this escalation action",
`src/commands/escalation_commands.py:52-56`), so a Resolve button in the dashboard would
always be refused. The page instead answers the decision (choice buttons, reply box) and
shows the collapsed terminal form when the escalation is resolved, cancelled or stale.
Sibling task `crisp-horizon-90.5` owns `aq escalation resolve`; the control can be added
when that command admits the operator.

**A batch is resolved from the delivery projection, not from a batch row.** No integration
batch is exposed on the dashboard wire (`openapi.json` has no batch read; `integration_status`
is CLI-only and refused to a worker token). The one projection that names a batch is
`EpicDeliveryStatus`, whose `responsible` / `links` carry `{kind: "batch", id}`, and the
graph list already sends it. The page scans each project's flat node list for that ref and
renders the epic, its delivery state, its pull request and its child tasks; a batch no
epic points at says so instead of guessing. This is the same client-side derivation the
desktop graph filter (`?batch=`) needs, and it is bounded by the server's `LIST_CAP` of 200
nodes per project.

## Screenshots

Chrome at a 390x844 phone viewport, this worktree's dev server, `/api` proxied to the
operator daemon on 127.0.0.1:8081. "live" = the box's own data; "fixture" = a stub daemon
answering `project_list`, the graph list, `supervisor_inbox_status` / `_history` and the
session messages, used where this box has nothing real to show.

| Route | Live | Fixture |
|---|---|---|
| `/focus/inbox` | [focus-inbox-live.png](focus-inbox-live.png) — 58 open escalations | — |
| `/focus/escalations/:id` | [focus-escalation-live.png](focus-escalation-live.png) — task link and reply box; [focus-escalation-options-live.png](focus-escalation-options-live.png) — choice buttons | — |
| `/focus/reviews/:id` | [focus-review-live.png](focus-review-live.png) — reader plus decision bar | — |
| `/focus/batches/:id` | [focus-batch-live-empty.png](focus-batch-live-empty.png) — no epic on this box names a batch today | [focus-batch-fixture.png](focus-batch-fixture.png) |
| `/focus/conversations` | [focus-conversations-live-disabled.png](focus-conversations-live-disabled.png) — `conversation_disabled`, `empty_allowlist`, `outbox_unbound` (spec §1.4) | [focus-conversations-list-fixture.png](focus-conversations-list-fixture.png), [focus-conversation-fixture.png](focus-conversation-fixture.png) |

## Checks

```
cd dashboard
npx vitest run src/pages/focus            # 12 files, 48 tests
npm run typecheck                          # tsc -b --noEmit
npx eslint src/pages/focus src/App.tsx
npm run build                              # tsc -b && vite build
```

Route coverage: `__tests__/FocusEscalation.test.tsx` (5), `FocusInbox.test.tsx` (3),
`FocusReview.test.tsx` (2), `FocusConversation.test.tsx` (5, list and detail),
`FocusBatch.test.tsx` (5), `routes.test.ts` (4), `focusFormat.test.ts` (4). From the
repository root:

```
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_dashboard_browser_storage.py   # 3 passed
```