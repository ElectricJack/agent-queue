# K10 handoff: Knowledge UI presentation slice

**Status:** delivered by `swift-grove-52` as the independent presentation slice of
K10 (`keen-rapids-64.1`) in the approved plan
`projects/agent-queue/plans/2026-10-01-aq-work-and-knowledge-records-implementation-plan.md`,
sections 10 and 12. **Date:** 2026-10-02.

This document is for whoever picks up the rest of K10: live API integration,
authorization-aware wiring, the transactional `knowledge_create_task`, and the
production route. Everything below is buildable and tested on `main` today with
**no** knowledge server code present (`main` has no `/api/knowledge/*`,
`/api/record/*` or `/api/link/*` routes; the generated TS client has no
knowledge types). Nothing here is routed, registered or reachable from the
shipped dashboard until K10 wires it.

## 1 What exists

All files are new; no shared or generated file was touched.

| File | Role |
|---|---|
| `dashboard/src/pages/knowledge/model.ts` | Typed view models: `KnowledgeListItemView`, `KnowledgeDetailView`, `KnowledgeHistoryEntryView`, `KnowledgeDiffView`, `KnowledgeSourceView`, `KnowledgeLinkView`, `KnowledgeCitationView`, `TaskKnowledgeView`, `KnowledgeEditDraft`, `KnowledgeUpdateInput`/`KnowledgeUpdateResult`, `AsyncView<T>`, the enum tuples (`KNOWLEDGE_CATEGORIES`, `KNOWLEDGE_LIFECYCLES`, `KNOWLEDGE_VERIFICATIONS`, `LINK_TYPES`, `KNOWLEDGE_ACTIONS`) and `DEFAULT_KNOWLEDGE_FILTERS`. |
| `dashboard/src/pages/knowledge/adapter.ts` | `KnowledgeAdapter` interface (`list`, `show`, `history`, `diff`, `update`, `taskKnowledge`), `KnowledgeAdapterError` with typed codes, `describeKnowledgeError` (fixed copy per code; never echoes server text). |
| `dashboard/src/pages/knowledge/fixtureAdapter.ts` | `createKnowledgeFixtureAdapter({ persona, pageSize, now })`: deterministic in-memory implementation with six records (verified+authoritative protected fact, three-revision incident, retired decision with successor, disputed+stale policy, record with a redacted revision, plain reference), persona-gated allowed actions, `if_revision` guard, idempotency receipts, `simulateExternalEdit`, `failWith`, `hold`, call log. |
| `dashboard/src/pages/knowledge/knowledgeUrlState.ts` | Pure URL helpers: `read/writeKnowledgeFilters` (`q`, `category`, `lifecycle` with `any`, `verification`), `read/writeKnowledgeSelection` (`record`, `revision`), `hasActiveKnowledgeFilters`. |
| `dashboard/src/pages/knowledge/useKnowledge.ts` | React Query hooks over an adapter (`useKnowledgeList` infinite, `useKnowledgeDetail`, `useKnowledgeHistory` infinite, `useKnowledgeDiff`, `useTaskKnowledge`, `useKnowledgeUpdate`), `knowledgeKeys`, and `listView`/`detailView`/`historyView`/`diffView`/`taskKnowledgeView` → `AsyncView`. A landed write invalidates `["knowledge", …]`; a conflict or refusal invalidates nothing. |
| `dashboard/src/pages/knowledge/KnowledgeBadges.tsx` | `KindBadge`, `CategoryBadge`, `LifecycleBadge`, `VerificationBadge`, `AuthorityBadge`, `StaleBadge`, `KnowledgeBadgeRow`. Text badges; each carries an sr-only facet label (`Lifecycle: retired`) and `data-badge="<facet>"`. |
| `dashboard/src/pages/knowledge/KnowledgeCard.tsx` | Selectable card (`aria-pressed`, `data-listnav`, `data-primary-control`, `data-knowledge-row`). No task controls. |
| `dashboard/src/pages/knowledge/KnowledgeFilters.tsx` | Labelled search + category/lifecycle/verification selects, "Clear filters". |
| `dashboard/src/pages/knowledge/KnowledgeList.tsx` | Loading (`role=status`), error (`role=alert`), empty (with reset), list with arrow-key navigation (`useListNav`) and "Load more". |
| `dashboard/src/pages/knowledge/Knowledge.tsx` | The surface: filters + list beside `KnowledgePane`. Props: `adapter`, `initialFilters`, `initialSelection`, `onStateChange`, `onOpenTask`, `onAction`, `heading`. Not routed. |
| `dashboard/src/panes/knowledge/KnowledgePane.tsx` | Detail view: header + badges, server-allowed actions, tabs Body / History / Provenance / Verification. Props: `adapter`, `recordId`, `revisionId`, `onRevisionChange`, `onOpenRecord`, `onOpenTask`, `onAction`. **No `manifest.ts` and no `index.tsx`**, so the pane registry and `tests/test_pane_registry_parity.py` do not see it. |
| `dashboard/src/panes/knowledge/KnowledgeActions.tsx` | Toolbar rendering only `detail.allowedActions`, fixed order; Edit disabled with a reason on a historical or redacted revision. |
| `dashboard/src/panes/knowledge/KnowledgeHistory.tsx`, `KnowledgeDiff.tsx` | Revision envelopes newest-first (`aria-current` on the viewed one, "View"/"View current", "Compare with previous" disabled across a redaction) and the two-revision comparison in the review pane's block style. |
| `dashboard/src/panes/knowledge/KnowledgeProvenance.tsx` | Sources with evidence labels (URL sources are text, never links); links with direction, type, endpoint kind badge, pinned/floating, resolution label, `data-edge-domain="informational"`; an unreadable endpoint shows no title and no control. |
| `dashboard/src/panes/knowledge/KnowledgeVerification.tsx` | Verification, authority and freshness stated separately. |
| `dashboard/src/panes/knowledge/KnowledgeEditForm.tsx`, `KnowledgeEditConflict.tsx` | Guarded edit: holds the observed revision token and one idempotency key; `conflict` blocks Save, offers "Reload current" (rebases the draft, user must save again) and "Compare" (exact diff observed → current). Never resubmits on its own. |
| `dashboard/src/panes/knowledge/TaskKnowledgePanel.tsx` | Informational task-side panel: pinned citations (always open the cited revision), readable links, "Save finding" enabled only with explicitly selected text. Styled like `TaskCollaboration`. |
| Tests | `pages/knowledge/__tests__/{Knowledge,knowledgeUrlState,fixtureAdapter}.test.*`, `panes/knowledge/__tests__/{KnowledgePane,TaskKnowledge}.test.tsx` — 39 tests. |

## 2 Contracts the live adapter must honour

Write `dashboard/src/pages/knowledge/liveAdapter.ts` (name is a suggestion)
implementing `KnowledgeAdapter` over the generated SDK once K03's routes are on
`main` and `./scripts/regenerate-ts-client.sh --from-file` has been run. The
components never import an adapter; they take one as a prop.

### 2.1 Reads → view models

| Plan command (§7) | Adapter method | Notes |
|---|---|---|
| `knowledge_list` | `list(filters, cursor)` → `KnowledgeListPage` | Metadata only. Map `search` row fields 1:1; `authoritative` = an active grant on the current revision; `stale`/`staleReason` = server freshness (set `staleReason: null` if the API has no reason string). `lifecycle: ""` means no lifecycle filter; the default is `"active"`. |
| `knowledge_show` | `show(recordId, revisionId)` → `KnowledgeDetailView` | `revisionId === null` is current. A pinned revision must resolve exactly: map `record.revision_unavailable` → `KnowledgeAdapterError("revision_unavailable")`, `record.revision_redacted` → a detail with `redacted: true`, `body: null`, `sources: []` **if** the envelope is readable, else `KnowledgeAdapterError("revision_redacted")`. `allowedActions` is the server's list; the UI adds nothing. `viewed.isCurrent` = `viewed.revisionId === current.revisionId`. |
| `knowledge_history` | `history(recordId, cursor)` → `KnowledgeHistoryPage` | Newest first. `redacted: true` for tombstones. |
| `knowledge_diff` | `diff(recordId, from, to)` → `KnowledgeDiffView` | Blocks `equal/added/removed` over the body; the review pane uses the same shape. |
| `link_list` + show's resolved link descriptors | part of `show` → `links: KnowledgeLinkView[]` | `endpoint.title === null` whenever the viewer may not read the endpoint; `resolution: "unauthorized"` in that case. Pinned task targets do not exist in v1. |
| task-side citations / links (K08 citations, K10 informational panel) | `taskKnowledge(taskId)` → `TaskKnowledgeView` | Citations are exact revisions; `isCurrent` compares with the record head. |

### 2.2 Writes → typed outcomes

`update(input)` returns, never throws for business outcomes:

| Server response | `KnowledgeUpdateResult` |
|---|---|
| `outcome: "updated"` | `{ outcome: "updated", revision }` |
| `outcome: "replayed"` | `{ outcome: "replayed", revision }` |
| `outcome: "unchanged"` | `{ outcome: "unchanged" }` |
| 409 `record.revision_conflict` | `{ outcome: "conflict", current }` — the token the server returned to an authorized reader |
| 403 forbidden capability/operation | `{ outcome: "forbidden", message }` |
| 422 invalid matrix/limits/snapshot | `{ outcome: "invalid", message }` |
| 428 missing precondition | should be unreachable: the form always sends `ifRevision`; treat as `invalid` |

The client interceptor throws on non-2xx, so the live adapter catches and maps
these; transport/5xx failures may still throw and the mutation surfaces them as
an error. `idempotencyKey` is generated once per form attempt and reused across
a conflict → reload → save sequence (failed attempts roll back their receipt).

### 2.3 Error codes for reads

`KnowledgeAdapterError.code`: `not_found` (404; denied and missing read the
same), `revision_unavailable` / `revision_redacted` (410), `disabled` (409
feature disabled), `forbidden` (403), `unavailable` (503/transport).
`describeKnowledgeError` turns these into fixed copy; do not pass server text
through to the page.

## 3 Mounting

1. **Route.** Add a lazy `/knowledge` route (and `routeChunks.ts` loader) that
   renders `<Knowledge adapter={live} … />`. Hide the navigation entry and refuse
   the route when `record_capabilities` reports UI disabled
   (`knowledge.ui_enabled=false`, or core/project disabled). Keep Work the
   command-center default. With `knowledge.writes_enabled=false` the server's
   `allowedActions` will omit `edit`/`retire`/…, and the UI is read-only without
   further work.
2. **URL state.** In the route component:
   ```ts
   const [params, setParams] = useSearchParams();
   <Knowledge
     initialFilters={readKnowledgeFilters(params)}
     initialSelection={readKnowledgeSelection(params)}
     onStateChange={({ filters, selection }) =>
       setParams((p) => writeKnowledgeSelection(writeKnowledgeFilters(p, filters), selection), { replace: true })}
   />
   ```
   Persistent preferences, if any, go through `useDashboardDocument` /
   `dashboard_state`; nothing in this slice touches browser storage
   (`tests/test_dashboard_browser_storage.py` stays green).
3. **Pane registration** (for agent pushes and the palette). Add
   `dashboard/src/panes/knowledge/manifest.ts` (`id: "knowledge"`, zod args
   `{ recordId: string; revisionId?: string }`) and `index.tsx` that adapts
   `PaneViewProps` to `KnowledgePane`, and the matching entry in
   `src/panes/registry.py` `SERVER_PANE_REGISTRY` — `tests/test_pane_registry_parity.py`
   enforces both sides.
4. **Task views.** In `dashboard/src/pages/TaskDetail.tsx` (details tab, after
   `<TaskCollaboration>`), and in `TaskWorkspace` if desired:
   ```tsx
   <TaskKnowledgePanel taskId={task.id} adapter={live}
     selectedText={selection} onOpenRecord={openKnowledge} onSaveFinding={saveFinding} />
   ```
   `selectedText` must be text the user explicitly selected; the panel never
   copies the task description itself.
5. **Actions.** `onAction(action, detail)` fires for `link`, `retire`,
   `restore`, `create_task`, `propose_correction`. Wire each to its flow:
   retire asks a reason; restore previews the resulting new revision and the
   verification reset; create task shows the ordinary routing/gate result and the
   `motivated_by` link and does not convert the record; propose correction opens
   the proposal lane. `edit` and `history` are handled inside the pane.
6. **Navigation out.** `onOpenTask(taskId)` → existing task detail;
   `onOpenRecord(recordId, revisionId)` is handled inside `Knowledge`.
   `KnowledgeProvenance` renders in-dashboard source `href`s as plain `<a>`;
   swap for `Link` or a callback when the route exists.

## 4 Invariants the components keep (and tests pin)

- No claim/complete/retry/push/priority/allocation/progress control on any
  knowledge surface; task controls stay in `TaskRowActions.tsx`.
- Kind, lifecycle and verification are separate text badges with sr-only facet
  labels; colour is never the only signal.
- A pinned revision resolves exactly or reports unavailable, and offers current
  only through an explicit control; it is never opened silently.
- A redacted revision is a tombstone: no body, no sources, no comparison.
- An endpoint the viewer may not read shows no title and no control.
- Links are informational (`data-edge-domain="informational"`); nothing feeds
  them into task layout, readiness or progress.
- Edit holds the observed token; a conflict blocks Save until an explicit
  reload and never retries against the new head.
- Markdown goes through `MarkdownPreview` (raw HTML escaped, `javascript:` hrefs
  dropped).

## 5 Verification commands (run on this slice)

```bash
cd dashboard
npx tsc -b --noEmit
npx eslint src/pages/knowledge src/panes/knowledge
VITEST_MAX_WORKERS=2 npx vitest run src/pages/knowledge src/panes/knowledge
cd ..
aq test tests/test_pane_registry_parity.py tests/test_dashboard_browser_storage.py tests/test_case_insensitive_paths.py
```

Dashboard tests do not run in CI (`.github/workflows/tests.yml` has no dashboard
step); these local runs are the evidence. Generate the TS client first in a
fresh worktree (`./scripts/regenerate-ts-client.sh --from-file`).

## 6 Not in this slice (deliberately)

- Live adapter, live hooks, any `fetch`/SDK call — main has no routes.
- Pane manifest/index, route, navigation entry, capability gating.
- Retire/restore/link/create-task/propose dialogs and their results (only the
  gated buttons and `onAction`).
- `RecordGraph.tsx`, mixed `kind=all` search, All records navigation (K11).
- Server work: `knowledge_create_task`, authority rules, migrations, shared
  context.

## 7 Known rough edges for K10

- Switching the pinned revision changes the detail query key, so the pane
  shows its loading line between revisions; `placeholderData: keepPreviousData`
  on `useKnowledgeDetail` would smooth that if wanted.
- The fixture's allowed-actions policy is a presentation stand-in for the plan's
  §4 table, not an authority rule; the live server is the only authority.
- `KnowledgeListItemView.staleReason` was added so the list badge can carry a
  tooltip; if `knowledge_list` returns no reason, map `null`.
