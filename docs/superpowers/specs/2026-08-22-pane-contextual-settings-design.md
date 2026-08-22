# Pane View — `contextual-settings` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:**
- `2026-08-22-dashboard-shell-v2-design.md` (shell primitives, `<ShellPane>`).
- `2026-08-22-pane-plugin-interface-design.md` (manifest schema, component
  contract, registry mechanics — **this spec implements that contract for
  one view and does not redefine it**).

## 1. Goal

Ship the `contextual-settings` pane view: a v3 ship-priority, polymorphic
settings editor. When the supervisor (or the user) is discussing a specific
entity — a project's config, an agent profile, a playbook's source, an
intelligence class — this view lets the user edit that entity inline in the
shell's right-side pane instead of navigating to `/settings/<section>` or
`/projects/:id/config`.

The view is keyed on a `subject` discriminant with five variants:
`project`, `profile`, `project-profile`, `playbook`, `intelligence-class`.
Each variant renders a thin, pane-sized wrapper around an **existing** edit
surface and **existing** save hook — no new form logic, no new mutation
endpoints.

## 2. Non-goals

- **Not a form-logic rewrite.** Every field list, validation rule, and save
  payload shape already exists in `Config.tsx` (project),
  `SystemProfileEditDrawer.tsx` / `ProfileEditDrawer.tsx` (profile /
  project-profile), and `PlaybookDetail.tsx`'s `SourceTab` (playbook). This
  view is a **THIN wrapper**: it re-renders the same field components
  (`IntelligenceClassPicker`, `McpServerSelector`, `ToolPicker`, a plain
  `<textarea>` for playbook source) wired to the same mutation hooks, laid
  out for the pane's width instead of a full page or a fixed-position
  drawer.
- **Not a Monaco integration.** No Monaco usage exists anywhere in the
  dashboard — `PlaybookDetail.tsx`'s "Source" tab is a plain `<textarea>`.
  This view matches that fidelity for `playbook`; no new editor dependency.
- **Not a routed detail page.** `/settings/profiles/:id` and
  `/settings/projects/:id` do not exist and this spec does not add them.
  This view is a second, pane-shaped consumer of the existing edit hooks,
  not a replacement for the existing drawer/page.
- **Not adding `repo_url` editing.** The assignment's field list for
  `project` includes `repo_url`, but `EditProjectRequest` has no such
  field — the daemon doesn't support editing it. `Config.tsx` renders it
  read-only; this view matches (see §13).
- **Not a hard requirement to redesign intelligence-class fetching**, but
  this view needs a read hook and none exists today —
  `IntelligenceClassPicker.tsx` and `IntelligenceClassesStub.tsx` each
  duplicate their own inline `legacyFetch` call. §12 extracts a shared
  `useIntelligenceClasses()` hook as a small dedup prerequisite, not a
  redesign of either existing consumer.
- **Not implementing the shell primitives** (`<ShellPane>`, `useShellPane`,
  `useShortcuts`, `useEntityShortcuts`) — assumed to exist per Phase B.
- **Not building the palette's args-prompting UX** (plugin-interface spec
  §11). Palette action opens with args pre-populated from obvious current
  focus, or is a no-op — see §6.3.

## 3. Manifest

```ts
// dashboard/src/panes/contextual-settings/manifest.ts
import { Cog6ToothIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";
import { contextualSettingsArgsSchema, type ContextualSettingsArgs } from "./args";

export const manifest: PaneManifest<ContextualSettingsArgs> = {
  id: "contextual-settings",
  name: "Settings",
  description: "Edit a project, profile, playbook, or intelligence class inline.",
  icon: Cog6ToothIcon,
  args_schema: contextualSettingsArgsSchema,
  open_shortcut: null,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open settings for…",
  palette_section: "Settings",
};
```

`icon` follows whatever heroicons component type `PaneManifest<TArgs>` in
`dashboard/src/panes/types.ts` settles on (see the same note in the
`diff-review-changes` spec §3 — `dashboard/CLAUDE.md` mandates heroicons
only). `route_scope: "cross-route"` is deliberate: a user mid-edit
navigating elsewhere to check something should not lose their draft — only
`Esc`-with-confirm or an explicit Save/Discard touches it (§8).
`open_shortcut: null` per the assignment — rare enough that palette +
agent-push cover it, and it means zero collision risk with any other
view's shortcut.

## 4. Args + validation

```ts
// dashboard/src/panes/contextual-settings/args.ts
import { z } from "zod";

const projectArgs = z.object({
  subject: z.literal("project"),
  subjectId: z.string().min(1), // project id
});
const profileArgs = z.object({
  subject: z.literal("profile"),
  subjectId: z.string().min(1), // system profile id (agent_type)
});
const projectProfileArgs = z.object({
  subject: z.literal("project-profile"),
  subjectId: z.string().min(1),  // agent_type
  projectId: z.string().min(1),
});
const playbookArgs = z.object({
  subject: z.literal("playbook"),
  subjectId: z.string().min(1), // playbook id
});
const intelligenceClassArgs = z.object({
  subject: z.literal("intelligence-class"),
  subjectId: z.string().min(1), // class id
});

export const contextualSettingsArgsSchema = z.discriminatedUnion("subject", [
  projectArgs, profileArgs, projectProfileArgs, playbookArgs, intelligenceClassArgs,
]);
export type ContextualSettingsArgs = z.infer<typeof contextualSettingsArgsSchema>;
```

Per plugin-interface spec §4/§6.1: an invalid `subject`, or a variant
missing required id field(s), fails `safeParse` at `open()` time — the
shell console-errors and no-ops before the component ever mounts. The
component's own switch (§5.1) only needs a compile-time exhaustiveness
check, not runtime guarding.

`subjectId` is uniform across all five variants (never `projectId`/
`profileId`/etc. as the discriminant key) so shared code (toolbar, header)
can read `args.subjectId` without a per-branch rename. `project-profile`
additionally carries `projectId` because that subject is addressed by the
`(projectId, agentType)` pair — matching `ProfileEditDrawer`'s own
`{ projectId, agentType }` props.

## 5. Component — per-subject renderer switch

```
dashboard/src/panes/contextual-settings/
├── manifest.ts
├── args.ts
├── index.tsx
├── useDirtyForm.ts
├── subjects/
│   ├── ProjectSubject.tsx
│   ├── ProfileSubject.tsx
│   ├── ProjectProfileSubject.tsx
│   ├── PlaybookSubject.tsx
│   └── IntelligenceClassSubject.tsx
└── __tests__/
    ├── index.test.tsx
    └── subjects.test.tsx
```

`index.tsx` is a thin switch with no field logic of its own:

```tsx
export default function ContextualSettingsPane(props: PaneViewProps<ContextualSettingsArgs>) {
  const { args } = props;
  switch (args.subject) {
    case "project":            return <ProjectSubject {...props} args={args} />;
    case "profile":             return <ProfileSubject {...props} args={args} />;
    case "project-profile":     return <ProjectProfileSubject {...props} args={args} />;
    case "playbook":            return <PlaybookSubject {...props} args={args} />;
    case "intelligence-class":  return <IntelligenceClassSubject {...props} args={args} />;
    default: { const _exhaustive: never = args; return _exhaustive; }
  }
}
```

`setArgs` is intentionally **not** used by any subject in v1 — none needs
in-pane navigation to a different subject/id. Reused components enumerated
below.

### 5.1 `project` — reuses `Config.tsx`

`FormState` and field list verbatim (source: `Config.tsx` lines 19-27):
`name`, `repo_default_branch`, `default_profile_id`, `max_concurrent_agents`,
`credit_weight`, `budget_limit`, `discord_channel_id`, plus a read-only
`repo_url` row. Always in edit mode (unlike `Config.tsx`'s page-level
view/edit toggle — the pane's whole purpose is inline editing).

Data: `useProject(subjectId)`, `useProjectProfiles(subjectId)` (populates
the `default_profile_id` picker, same dedup-across-scoped/global logic
`Config.tsx` uses). Save: `useEditProject()` with `Config.tsx`'s exact
payload shape (`parseOptionalInt`/`parseOptionalFloat` helpers, promoted
to named exports — §12).

### 5.2 `profile` — reuses `SystemProfileEditDrawer.tsx`

Renders the same body content — Basics, Intelligence class & permissions
(`IntelligenceClassPicker`), System prompt suffix, MCP servers
(`McpServerSelector`), Allowed tools (`ToolPicker`) — **not** the drawer
component itself (its `fixed inset-0` overlay + slide-in `<aside>` chrome
is wrong inside a pane body; see §13). `Section`/`Field` local layout
helpers, currently duplicated verbatim between `SystemProfileEditDrawer.tsx`
and `ProfileEditDrawer.tsx`, are promoted to a shared
`dashboard/src/components/profile/FormSection.tsx` all three consumers
import (§12).

Data: `useGetProfile(subjectId)`, mapped through `profileToForm` (promoted
to a shared export, §12). Save: `useEditProfile()` with the identical
request shape `SystemProfileEditDrawer.onSave` builds, including its
existing `default_class`/`mcp_servers` type-loosening casts (stale
generated types, not a new workaround).

### 5.3 `project-profile` — reuses `ProfileEditDrawer.tsx`

Same reuse pattern as §5.2. `useProjectProfiles(projectId)`, find the row
where `agent_type === subjectId`, seed from `row.scoped ?? row.global`.
Save via `useEditProjectProfile()` with `{ project_id: projectId, agent_type: subjectId, ... }`.

Carried-over behavior: when no `row.scoped` exists (only `row.global`
seeds the form), `[Save]` stays disabled and a banner reads "No project
override exists yet" — matching `ProfileEditDrawer.tsx`'s own
`disabled={!scoped || edit.isPending}`. No "create override" flow exists
in the source component; this view doesn't invent one (§13).

### 5.4 `playbook` — reuses `PlaybookDetail.tsx`'s `SourceTab`

Plain `<textarea>` bound to a local `draft` string, `dirty = draft !==
source.markdown`, save via `useUpdatePlaybookSource().mutateAsync({
playbook_id, markdown: draft, expected_source_hash: baseHash })` with the
same optimistic-concurrency conflict handling and post-save compile-result
surfacing (`lastResult.compiled`, `.errors`) as the source component.

No "Compiled"/"Runs" tab — those stay page-only. Optional preview toggle
(per the assignment) shows the same node-count/trigger summary the page
header renders, sourced from `usePlaybooks()` filtered to this id —
read-only, no new data source.

Data: `usePlaybookSource(subjectId)`, `usePlaybooks()`. Save:
`useUpdatePlaybookSource()`.

### 5.5 `intelligence-class` — read-only

Fetches via the new `useIntelligenceClasses()` (§7, §12) and renders the
single matrix row matching `subjectId` — same per-provider table shape
`IntelligenceClassPicker.tsx`/`IntelligenceClassesStub.tsx` already
render, scoped to one id. Below it, a static hint:

> These classes ship from the vault. Edit
> `vault/intelligence-classes/<id>.md` to change them.

No mutation hook, no `[Save]`/`[Discard changes]` toolbar entries (§6.1).

### 5.6 Shared dirty-state hook

```ts
// dashboard/src/panes/contextual-settings/useDirtyForm.ts
function useDirtyForm<T>(initial: T) {
  const [value, setValue] = useState(initial);
  const [baseline, setBaseline] = useState(initial);
  const dirty = !deepEqual(value, baseline); // form shapes are flat/serializable
  const resetBaseline = (next: T) => { setValue(next); setBaseline(next); };
  return { value, setValue, dirty, resetBaseline };
}
```

Used once per editable subject (all but `intelligence-class`). `dirty`
feeds the toolbar's `[Save]` disabled state and the header's dirty
indicator (§8).

## 6. Toolbar + shortcuts

### 6.1 Toolbar

```ts
setToolbar([
  { id: "save",    label: "Save",                icon: CheckIcon,               onClick: save,    disabled: !dirty || saveMutation.isPending },
  { id: "discard", label: "Discard changes",      icon: ArrowUturnLeftIcon,      onClick: () => resetBaseline(originalFromServer), disabled: !dirty },
  { id: "open-full", label: "Open full settings page", icon: ArrowTopRightOnSquareIcon, onClick: () => navigate(fullSettingsRoute(args)) },
]);
```

`intelligence-class` registers only `open-full` — no `save`/`discard`.

### 6.2 `fullSettingsRoute(args)`

None of the four editable subjects have a routed detail page keyed by id
(§2) — `[Open full settings page]` navigates to the closest existing
route:

| `subject`            | Route                              | Note |
|------------------------|-------------------------------------|------|
| `project`              | `/projects/<subjectId>/config`     | Exact match. |
| `profile`              | `/settings/profiles`               | List page; no id pre-selected — the list page's drawer isn't auto-opened (§13). |
| `project-profile`      | `/projects/<projectId>/profiles`   | Same caveat. |
| `playbook`              | `/playbooks/<subjectId>`            | Exact match — lands on `PlaybookDetail`, defaults to its own "Source" tab already. |
| `intelligence-class`    | `/settings/intelligence-classes`   | List page; specific class not scrolled-to (no such affordance exists today). |

Three of five subjects land on a list rather than the specific item —
called out explicitly since it's the most visible seam of "no new detail
routes" (§2); flagged again in §13 as a natural v2 follow-up.

### 6.3 Palette invocation

Per the assignment: opens with pre-populated args only if current focus
makes the subject obvious; otherwise no-op for v1 (full args-prompting is
deferred, plugin-interface spec §11).

Focus-resolution, checked when the palette action fires:

1. Route is `/projects/:projectId/config` → opens `{ subject: "project", subjectId: projectId }`.
2. Route is `/playbooks/:playbookId` → opens `{ subject: "playbook", subjectId: playbookId }`.
3. Route is `/projects/:projectId/profiles` with a row focused, or
   `/settings/intelligence-classes` with a row focused → **not implemented
   in v1** (would need those pages to expose focus/selection state, which
   isn't exposed today) — falls through.
4. None of the above → **no-op**. The action still appears in the palette
   (so users learn it exists); firing it shows a toast ("Open Settings
   from a project, playbook, or profile page, or ask the supervisor to
   open it.") rather than opening an empty pane.

Case 3 is the one most worth building focus-tracking for in a v2 pass
(§13).

### 6.4 Shortcuts

```ts
setShortcuts([
  { key: "$mod-s", label: "Save",            onFire: () => dirty && save() },
  { key: "Escape",  label: "Discard & close", onFire: handleEscape },
]);

function handleEscape() {
  if (!dirty) { close(); return; }
  if (window.confirm("Discard unsaved changes to this settings pane?")) close();
  // else: no-op, draft stays intact
}
```

`intelligence-class` doesn't register `$mod-s` (nothing to save).
`window.confirm` matches the lighter discard-confirm pattern already used
elsewhere in the dashboard for reversible client-side state (server data
is untouched either way).

## 7. Data + queries — one hook per subject; save path

| `subject`            | Read hook(s)                                                               | Save hook                  |
|------------------------|-------------------------------------------------------------------------------|-----------------------------|
| `project`              | `useProject(subjectId)`, `useProjectProfiles(subjectId)`                    | `useEditProject()`          |
| `profile`              | `useGetProfile(subjectId)`                                                   | `useEditProfile()`          |
| `project-profile`      | `useProjectProfiles(projectId)` (row lookup by `agent_type === subjectId`) | `useEditProjectProfile()`   |
| `playbook`              | `usePlaybookSource(subjectId)`, `usePlaybooks()`                            | `useUpdatePlaybookSource()` |
| `intelligence-class`    | `useIntelligenceClasses()` (**new**, §12)                                   | — (read-only)               |

Every hook except `useIntelligenceClasses()` already exists in
`dashboard/src/api/hooks.ts` (confirmed signatures: `useProject` L309,
`useEditProject` L354, `useGetProfile` L440, `useEditProfile` L454,
`useProjectProfiles` L731, `useEditProjectProfile` L775,
`usePlaybookSource`/`useUpdatePlaybookSource` L627-662). The new hook is a
straight extraction of logic already duplicated twice (`IntelligenceClassPicker.tsx`,
`IntelligenceClassesStub.tsx`) — the endpoint already exists and is
already called from the frontend, just not through a shared hook:

```ts
// new, dashboard/src/api/hooks.ts
export function useIntelligenceClasses() {
  return useQuery({
    queryKey: ["intelligence-classes"],
    queryFn: async () => legacyFetch("/api/system/list-intelligence-classes") as Promise<IntelligenceClassesResponse>,
  });
}
```

Both existing call sites switch to this hook instead of their own inline
`legacyFetch` (§12) — net reduction from two duplicated fetches to one.

**Save path:** each editable subject's `save()` calls its mutation with
the exact payload shape its source component already builds (no shared
"generic save" abstraction — the five payload shapes have nothing in
common). On success: `resetBaseline(newValue)` (clears dirty state without
closing the pane, matching `diff-review-changes`'s precedent of not
auto-closing on success) plus a brief inline "Saved" flash in the header.
On failure: the thrown error renders in a red banner, matching
`SystemProfileEditDrawer.tsx`'s `fatal` state exactly.

## 8. Save + dirty tracking + confirm-discard

- Dirty tracking: `useDirtyForm` (§5.6), one instance per editable
  subject, comparing current form value to the last-loaded-or-saved
  baseline.
- Dirty indicator: a small filled dot next to the pane header's title when
  `dirty === true`. The shell's header (plugin-interface spec §5.5)
  doesn't currently accept a `dirty` prop — this view renders its own
  indicator inline rather than requiring a shell change (§13 flags this as
  a candidate for a shared shell-header addition if more views want it).
- Save (`$mod-s` or toolbar): disabled while `!dirty` or
  `saveMutation.isPending`.
- Discard: toolbar `[Discard changes]` resets the form but does **not**
  close the pane; `Esc` resets **and** closes, after the confirm (§6.4).
  This distinction mirrors each action's own label.
- Route changes never trigger a discard prompt (§3) — only `Esc` and
  explicit toolbar actions touch the draft.

## 9. Loading + error + not-found cases

Branch order per subject, matching `diff-review-changes` spec §8's
precedent and each source component's existing branches:

1. **Loading** — `"Loading <subject>…"`, `text-sm text-gray-500`.
2. **Query error** (403/404/etc. from the underlying `get_project` /
   `get_profile` / `get_playbook_source` call) — red error text with the
   thrown message, rendered in place of the form body. Every generated
   client call in `hooks.ts` uses `throwOnError: true`, so unknown ids
   collapse into this branch rather than needing a distinct "not found"
   path — no client-side status-code differentiation added, matching every
   other page in the dashboard.
3. **`project-profile` with no scoped override** — not an error; renders
   the form seeded from `global` with `[Save]` disabled and the inherited
   banner (§5.3), not a new branch invented for this view.
4. **`intelligence-class` id not present in the fetched list** — the one
   subject where "not found" is a genuine client-side filter miss (the
   list itself loads fine): renders "Intelligence class `<id>` not found."
   in place of the matrix.

## 10. Agent-push examples

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "here's demo's config — bump the budget limit whenever you're ready →" \
    --pane-open '{"view": "contextual-settings", "args": {"subject": "project", "subjectId": "demo"}}'

aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "here's the reviewer profile you're editing →" \
    --pane-open '{"view": "contextual-settings", "args": {"subject": "profile", "subjectId": "reviewer"}}'

aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "demo's override for the coder profile →" \
    --pane-open '{"view": "contextual-settings", "args": {"subject": "project-profile", "subjectId": "coder", "projectId": "demo"}}'

aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "the review-gate playbook failed to compile — here's the source →" \
    --pane-open '{"view": "contextual-settings", "args": {"subject": "playbook", "subjectId": "review-gate"}}'

aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "that's the \"fast\" class — here's what it resolves to per provider →" \
    --pane-open '{"view": "contextual-settings", "args": {"subject": "intelligence-class", "subjectId": "fast"}}'
```

All five are `agent_pushable: true` — auto-open on arrival subject to
`to_kind === "user"` (plugin-interface spec §6.5).

Server-side mirror entry (plugin-interface spec §7, option A):

```python
# src/panes/registry.py
SERVER_PANE_REGISTRY = { ..., "contextual-settings": {"agent_pushable": True} }
```

## 11. Tests

`__tests__/index.test.tsx` — manifest/args:
- `manifest.id === "contextual-settings"`; `manifest.open_shortcut === null`.
- `contextualSettingsArgsSchema` accepts all five valid shapes; rejects
  `{ subject: "project" }` (missing `subjectId`), `{ subject: "bogus", subjectId: "x" }`,
  and `{ subject: "project-profile", subjectId: "x" }` (missing `projectId`).
- Exhaustiveness is a compile-time check via the `never` assignment in
  §5.1, not a runtime test.

`__tests__/subjects.test.tsx` — one block per subject (RTL +
`QueryClientProvider`, mocked hooks):
- `project`: renders `Config.tsx`'s field set; `repo_url` is read-only;
  editing enables `[Save]`; save payload matches `Config.tsx`'s shape;
  `[Discard changes]` reverts and re-disables `[Save]`.
- `profile`: renders the same sections as `SystemProfileEditDrawer`
  (Basics, Intelligence class & permissions, System prompt suffix, MCP
  servers, Allowed tools); save payload matches `onSave`'s shape including
  the `mcp_servers` cast.
- `project-profile`: seeds from `scoped`, falls back to `global`; `[Save]`
  disabled with no override, banner visible.
- `playbook`: textarea renders `source.markdown`; save calls
  `useUpdatePlaybookSource` with `expected_source_hash` equal to the
  loaded hash; a mocked `conflict` response surfaces without clobbering
  the draft.
- `intelligence-class`: renders only the matching row, the vault hint, and
  a 1-action toolbar (`open-full` only, no save/discard).

Shared behavior (any one subject as fixture, per plugin-interface spec
§9.1):
- `$mod-s` fires `save()` when dirty, no-ops otherwise.
- `Esc` with `!dirty` closes immediately, no confirm.
- `Esc` with `dirty` triggers `window.confirm`; accept closes; decline
  leaves the draft intact.
- `[Open full settings page]` navigates per §6.2's table (parametrized,
  5 cases).
- Loading / query-error / not-found branches (§9) each render their own
  placeholder and suppress the form body.

**Registry:** standard entries in `dashboard/src/panes/__tests__/registry.test.ts`
(plugin-interface spec §9.2) — resolves, unique id, no shortcut collision
(trivial — `null`), parity with `src/panes/registry.py`.

## 12. Implementation checklist

- [ ] Create `dashboard/src/panes/contextual-settings/` directory (§5).
- [ ] Write `args.ts` with the discriminated union (§4).
- [ ] Write `manifest.ts` (§3), using the shared `PaneManifest` type from
      `dashboard/src/panes/types.ts` (create it if no earlier view has
      landed one yet).
- [ ] Promote `Config.tsx`'s local `FormState` type and
      `parseOptionalInt`/`parseOptionalFloat` to named exports (§5.1).
- [ ] Extract the duplicated `Section`/`Field` components out of
      `SystemProfileEditDrawer.tsx` and `ProfileEditDrawer.tsx` into a
      shared `dashboard/src/components/profile/FormSection.tsx`; update
      both drawers to import from it (§5.2) — net reduction, not addition.
- [ ] Promote the (duplicated) `profileToForm` mapper to a single shared
      export alongside `FormSection`.
- [ ] Add `useIntelligenceClasses()` to `dashboard/src/api/hooks.ts` (§7);
      update `IntelligenceClassPicker.tsx` and `IntelligenceClassesStub.tsx`
      to consume it instead of their own inline fetches.
- [ ] Write `subjects/ProjectSubject.tsx`, `ProfileSubject.tsx`,
      `ProjectProfileSubject.tsx`, `PlaybookSubject.tsx`,
      `IntelligenceClassSubject.tsx` (§5.1–5.5).
- [ ] Write `useDirtyForm.ts` (§5.6).
- [ ] Write `index.tsx`'s subject switch with the exhaustiveness guard (§5).
- [ ] Wire toolbar (§6.1–6.2) and shortcuts (§6.4) per subject.
- [ ] Register palette focus-resolution, cases 1–2 only for v1 (§6.3).
- [ ] Add `"contextual-settings": {"agent_pushable": True}` to
      `src/panes/registry.py`.
- [ ] Write `__tests__/index.test.tsx` and `__tests__/subjects.test.tsx` (§11).
- [ ] Run the shared frontend registry test and
      `tests/test_pane_registry_parity.py` once both exist.

## 13. Open questions

- **Drawer-vs-embeddable-form refactor.** This spec resolves the
  assignment's open question by choosing *not* to render
  `SystemProfileEditDrawer`/`ProfileEditDrawer` wholesale — their overlay
  chrome is wrong inside a pane body. Instead it extracts just the two
  duplicated inner pieces (`Section`/`Field`, `profileToForm`) into shared
  exports. A fuller extraction (a standalone `ProfileFormFields` component
  both drawers and this pane render inside their own chrome) remains a
  live option if a third consumer shows up — not done here since two
  extractions fully unblock this view without speculative work.
- **List-page deep-linking.** Three of five subjects (§6.2) land
  `[Open full settings page]` on a list, not the specific item, because
  `/settings/profiles/:id`, `/projects/:id/profiles/:agentType`, and a
  scroll-to-class affordance don't exist. A natural v2 follow-up: build
  those routes, or have the list pages accept a `?open=<id>` param this
  view's action could pass. Out of scope for a pane-view spec to add new
  routes to pages it doesn't own.
- **Palette focus-resolution gaps** (§6.3, case 3). Needs
  `ProjectProfiles`/`IntelligenceClassesStub` to expose focus/selection
  state via a shared primitive (e.g. `useFocusedEntity()`) — a shell-level
  addition, not something this view can add unilaterally. Flagged for
  whoever tackles the plugin-interface spec's deferred palette
  args-prompting follow-up (§11 there).
- **Dirty indicator in the shared pane header.** §8 — the shell's pane
  header doesn't accept a `dirty` flag today; this view renders its own.
  Worth raising with whoever lands the shell header if more views want the
  same affordance.
- **`project-profile`'s no-override state has no "create override"
  action** — inherited from `ProfileEditDrawer.tsx` (§5.3), not introduced
  here. If that drawer grows one, this subject picks it up via the same
  reuse pattern.
- **`repo_url` has no edit path today.** If the daemon later adds one,
  `Config.tsx` and this subject both need the same one-line addition —
  tracked here so it isn't assumed "already handled."
