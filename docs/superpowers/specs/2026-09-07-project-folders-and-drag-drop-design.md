# Project folders and drag/drop in the left rail

<!-- aq:historical -->
> **Historical design record.** This spec describes one feature as it was
> designed, not as the code stands today. Start at [the documentation
> home](../../README.md) for current behaviour; see [historical
> material](../../history/README.md).

**Task:** `calm-grove` — "I want to be able to organize my projects in the nav
bar on the left. Need to be able to add folders and drag projects around."

**Status:** implemented.

## 1. Problem

`dashboard/src/shell/LeftRail.tsx` renders every project returned by
`useProjects()` as one flat list in API order. With more than a handful of
projects the rail becomes an undifferentiated scroll, and the operator has no
way to express which projects belong together or which ones they care about
today.

## 2. Scope

- Named folders in the Projects section of the left rail: create, rename,
  delete, collapse/expand.
- Drag a project into a folder, out of a folder, or to a new position in a
  list.
- Drag a folder to reorder it among the other folders.
- A keyboard- and screen-reader-reachable equivalent for every drag gesture.

Out of scope: nested folders, per-folder colours/icons, sharing an
organization between browsers or users, and any server-side model.

## 3. Where the organization lives

The organization is **per-browser view state, stored in `localStorage`** under
`aq.shell.project-organization`, following the pattern already used for graph
positions (`layout-v2/manualPositions.ts`), pane state (`panes/store.tsx`) and
density. Rationale:

- It is a view preference, not domain state: nothing in the daemon,
  scheduler, playbooks or CLI reads it, and no other surface renders it.
- A server model would mean a `tables.py` change, an alembic migration, API
  models, and a regeneration of both generated clients — a large, permanent
  schema commitment for a sidebar nicety.
- It fails safe. Unparseable or absent storage yields the empty organization,
  which renders exactly today's flat list.

Consequence, accepted: the organization does not follow the operator to
another browser or machine. If it later needs to, the storage module is the
single seam to swap for an API-backed one — the tree projection and all the
operations are already pure functions over a plain value.

## 4. Model

`dashboard/src/shell/navOrganization.ts` owns a plain serialisable value:

```ts
interface NavOrganization {
  folders: { id: string; name: string; collapsed: boolean }[]; // rail order
  assignments: Record<string, string>;                         // projectId -> folderId
  order: string[];                                             // projectId rail order
}
```

`order` is one global ranking of project ids rather than a per-folder list.
Rendering filters it by folder, so relative order inside a folder is exactly
the relative order in `order`; moving a project between folders never has to
rewrite two lists, which removes the class of bugs where a project is in two
folders or in none.

The stored value is **advisory**. `navTree(projects, org)` reconciles it with
the live project list on every render:

- a project the organization has never seen sorts after the ranked ones, in
  API order, at the rail root;
- an id in `order` or `assignments` that is no longer a project is ignored,
  never rendered, and never deleted from storage (a project that disappears
  because the daemon is mid-restart keeps its place when it returns);
- an assignment to a folder that no longer exists renders at the root.

All mutations are pure `(org, ...args) => NavOrganization`:
`createFolder`, `renameFolder`, `deleteFolder` (its projects fall back to the
root, keeping their relative order), `toggleFolder`, `moveProject`
(`{ folderId, beforeProjectId }`) and `moveFolder`.

## 5. Interaction

Drag/drop is native HTML5 DnD — the dashboard ships no DnD library, and the
gestures here are a single-level list. Drop targets:

| target | effect |
| --- | --- |
| a folder header | append the project to that folder |
| a project row | place the dragged project before it, in that row's folder |
| the root drop zone under the list | move the project out to the root |
| a folder header, dragging a folder | place the dragged folder before it |

`dataTransfer` carries a private MIME type per kind (`application/x-aq-nav-project`,
`application/x-aq-nav-folder`) so a folder drag is never read as a project
drop, plus a `text/plain` fallback for the browsers that insist on one.

**Accessibility.** Drag/drop is a pointer gesture and cannot be the only path.
Every project row carries an always-in-DOM `<select>` labelled
"Move <project> to folder" (revealed on hover/focus) listing the root and each
folder, and every folder header carries "Move folder up" / "Move folder down"
buttons. Both drive the same pure operations as the drop handlers. Folder
headers are disclosure buttons with `aria-expanded`/`aria-controls`, matching
the existing Projects disclosure, and stay in the rail's `data-listnav` arrow
navigation.

## 6. Files

- `dashboard/src/shell/navOrganization.ts` — value, storage, projection, operations.
- `dashboard/src/shell/useNavOrganization.ts` — React state over the storage,
  synchronised across tabs and rail instances via `storage` and a same-document
  `aq:project-organization-changed` event.
- `dashboard/src/shell/ProjectTree.tsx` — the Projects section of the rail.
- `dashboard/src/shell/LeftRail.tsx` — renders `ProjectTree`, keeps ownership of
  the disclosure, the Add-project wizard and the onboarding hooks.

## 7. Tests

- `dashboard/src/shell/navOrganization.test.ts` — projection
  reconciliation and every operation, including the malformed-storage paths.
- `dashboard/src/shell/ProjectTree.test.tsx` — creating/renaming/
  deleting folders, drag between folders and roots, folder reordering, and the
  keyboard equivalents.
- `dashboard/src/shell/LeftRail.addProject.test.tsx` — unchanged; the existing
  rail contract must keep passing.
