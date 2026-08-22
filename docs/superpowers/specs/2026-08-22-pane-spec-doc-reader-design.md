# Pane View: `spec-doc-reader` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell
primitives), `2026-08-22-pane-plugin-interface-design.md` (pane
contract — every section below implements that contract for this one
view).
**Ship priority:** v3 (Phase C, third wave — after v1: task-detail,
diff-review-changes, file-browser; and v2: session-peek, console,
playbook-run-inspector).

## 1. Goal

A markdown-rendered reader for a spec or design doc, opened either by
the user (palette, file-browser click-through) or pushed by a
supervisor mid-conversation ("here's the spec →"). Optimized for
long-form reading: a table of contents for quick jumps, sticky section
context while scrolling, and a compact frontmatter summary instead of
raw YAML dumped into the body.

Close cousin of file-browser's preview — same `MarkdownPreview`
component and (for local files) the same file-fetch endpoint — but
tuned for reading one long document top-to-bottom rather than browsing
a tree of files.

## 2. Non-goals

- Not a markdown editor. Read-only.
- Not a diff view — that's `diff-review-changes`.
- Not a generic renderer for non-markdown formats. Non-markdown gets a
  plain monospace fallback (§8.2), nothing richer.
- Not a spec index/browser. This view renders one document; finding
  which one is the palette's or file-browser's job.
- No validation against a fixed frontmatter schema — whatever YAML is
  present is rendered permissively (§5.3).

## 3. Manifest

```ts
// dashboard/src/panes/spec-doc-reader/manifest.ts

import { z } from "zod";
import { BookOpenText } from "lucide-react";
import type { PaneManifest } from "../types";

const argsSchema = z
  .object({
    workspaceId: z.string().optional(),
    path: z.string().optional(),
    url: z.string().optional(),
  })
  .superRefine((val, ctx) => {
    const hasWorkspacePath = val.workspaceId !== undefined && val.path !== undefined;
    const hasUrl = val.url !== undefined;

    if (hasWorkspacePath === hasUrl) {
      // both present, or neither present — reject either way
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "requires exactly one of (workspaceId + path) or url",
      });
      return;
    }
    if (val.workspaceId !== undefined && val.path === undefined) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["path"], message: "path is required when workspaceId is set" });
    }
    if (val.path !== undefined && val.workspaceId === undefined) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["workspaceId"], message: "workspaceId is required when path is set" });
    }
  });

export type SpecDocReaderArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<SpecDocReaderArgs> = {
  id: "spec-doc-reader",
  name: "Spec Reader",
  description: "Read a spec or design doc with table of contents and frontmatter summary.",
  icon: BookOpenText,
  args_schema: argsSchema,
  open_shortcut: null,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Read spec",
  palette_section: "Docs",
};
```

`route_scope` stays at the `"cross-route"` default — a user reading a
spec while navigating the dashboard is exactly the persistence case
the shell was built for. `palette_label` registers a discoverable
action; per interface-spec §11 (deferred args-prompting), invoking it
from the palette with no prior focus context is a documented no-op
today (console warning) until args-prompting ships.

## 4. Args + validation

```ts
interface SpecDocReaderArgs {
  workspaceId?: string; // required together with `path`
  path?: string;        // required together with `workspaceId`
  url?: string;          // required alone
}
```

**Mutual exclusion**, enforced in the zod schema's `superRefine` (§3),
not the component — matches the interface spec's contract that
`open()` validates before the component ever mounts:

- `(workspaceId, path)` both present, `url` absent → local-file mode.
- `url` present alone → hosted-doc mode.
- Any other combination — both groups present, only one of
  `workspaceId`/`path`, or neither group — is rejected.

Examples:

```ts
open("spec-doc-reader", {
  workspaceId: "ws-project-repo-demo",
  path: "docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md",
});

open("spec-doc-reader", { url: "/api/specs/2026-08-22-dashboard-shell-v2-design.md" });
```

## 5. Component

### 5.1 Layout

```
┌ [book-icon] Spec Reader ──────────── [Copy] [Editor] [Full-page] × ┐
│ ┌──────────┐ ┌──────────────────────────────────────────────────┐  │
│ │ Status:   │ │  Dashboard Shell v2 — Design      (resolved title)│ │
│ │  design   │ │                                                  │  │
│ │ Date:     │ │  ## 1. Goal                                      │  │
│ │  2026-... │ │  ...body...                                      │  │
│ │ Companion:│ │                                                  │  │
│ │  pane-... │ │  ## 2. Non-goals  ...                            │  │
│ ├──────────┤ │                                                  │  │
│ │ TOC       │ │                                                  │  │
│ │ 1 Goal    │ │                                                  │  │
│ │  ...      │ │                                                  │  │
│ └──────────┘ └──────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

The shell-provided header (icon + name + close, per interface spec
§5) keeps the static manifest name ("Spec Reader"); the resolved
document title (§5.4) renders as the first element of the view's own
content, styled as a document title, above the meta card.

Below the title: the frontmatter meta card (§5.3), only when
frontmatter is present. Below that, a two-column body once pane width
≥ 420px — a sticky TOC sidebar (left, ~180px) and the markdown body
(right). Below 420px, the TOC collapses into a `<details>` disclosure
above the body instead of a side column, reusing the same list markup.

TOC entries come from `##` and `###` headings only. `#` is reserved
for the doc title and excluded; `###` renders indented under its
parent `##`; `####`+ is excluded to keep the TOC short on deeply
nested docs.

### 5.2 TOC extraction

Derived once per document load from the same markdown AST
`MarkdownPreview` already builds (`remark`/`rehype`, with
`rehype-slug` producing heading `id`s) — not reimplemented, reused:

```ts
interface TocEntry { id: string; text: string; depth: 2 | 3 }
function extractToc(markdownSource: string): TocEntry[];
```

Walks `heading` nodes with `depth === 2 || 3`, slugifies with the same
slugger `rehype-slug` uses (`github-slugger`) so TOC ids agree exactly
with the rendered body's heading ids. Computed via `useMemo` keyed on
source text.

### 5.3 Frontmatter

Parsed with `gray-matter` (splits `{ data, content }`) when a fenced
`---` YAML block opens the file. No fixed schema for `data`:

- Known keys (`status`, `date`, `companion_specs`/`companions`) get
  friendly rendering — status as a pill, date formatted,
  companions as links that open another `spec-doc-reader` instance
  (via `setArgs` for a sibling path, or `open()` for a different mode).
- Unknown keys render as plain `label: value` rows, in order.
- Empty/absent `data` → the meta card is omitted, not shown empty.

This repo's specs (this file included) don't use fenced frontmatter —
they use a bold-label preamble (`**Status:** ...`). When no YAML block
is found, a fallback heuristic scans lines before the first `##`
heading for `^\*\*([^*]+):\*\*\s*(.+)$` and feeds matches into the
same compact meta-card component, so today's spec corpus gets the
card without retrofitting every doc.

### 5.4 Title resolution

1. Frontmatter `title` or `name` (prefer `title` if both present).
2. First `#` (h1) heading in the body.
3. Fallback: filename (local-file mode) or URL's last path segment
   (url mode), humanized.

## 6. Toolbar + shortcuts

### 6.1 Toolbar (`setToolbar`)

- **Copy path/url** — copies `path` (local-file) or `url` (url mode);
  label adapts ("Copy path" / "Copy URL").
- **Open in editor** — only in local-file mode. No browser filesystem
  handle exists to shell out to a real editor, so this copies the
  path with a toast: "Path copied — open in your editor."
- **Open full-page view** — only when the doc resolves to a route the
  dashboard already has. Detection: `path`/`url` matches
  `vault/**/playbooks/*.md`, or frontmatter carries `playbook_id` →
  navigates to `/settings/playbooks/<playbook_id>`. For the common
  case (a spec under `docs/superpowers/specs/`) no full-page route
  exists today, so the button is simply omitted — see open question
  §12.

Order: Copy path/url, Open in editor (if applicable), Open full-page
view (if applicable).

### 6.2 Shortcuts (`setShortcuts`)

| Key | Action |
|---|---|
| `↑` / `↓`, `j` / `k` | Scroll body by a line |
| `PgUp` / `PgDn` | Scroll body by one viewport page |
| `t` | Focus TOC (first entry, or the currently-active one) |
| `Enter` (TOC focused) | Scroll body to that heading, mark active, return focus to body |

All scrolling targets the pane's own inner scroll container, not the
page. Bindings fire only while the pane holds focus (interface spec
§5.2), so this view's `t` doesn't collide with the shell's chat-
composer `t` (which only fires from main content on `/`).

### 6.3 Anchor navigation

TOC entries are `<button>`s, not `<a href="#...">` — activating one
calls `scrollIntoView({ behavior: "smooth", block: "start" })` on the
target heading with no `history.pushState` and no hash change, per the
brief ("URL doesn't need to change"). Active-section highlighting uses
an `IntersectionObserver` scoped to the pane's scroll container
(`root` option) rather than the window.

## 7. Data + queries

### 7.1 Local-file mode

Reuses file-browser's endpoint: `GET
/api/workspaces/{workspaceId}/file?path=<path>` → `{ path, content,
size, truncated }`.

```ts
function useWorkspaceFile(workspaceId: string, path: string) {
  return useQuery({
    queryKey: ["workspace-file", workspaceId, path],
    queryFn: () => api.get(`/api/workspaces/${workspaceId}/file`, { params: { path } }),
    staleTime: 30_000,
  });
}
```

### 7.2 Url mode

Direct same-origin `fetch()`, wrapped in `useQuery` for consistent
loading/error/retry semantics:

```ts
function useHostedDoc(url: string) {
  return useQuery({
    queryKey: ["hosted-doc", url],
    queryFn: async () => {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new HostedDocFetchError(res.status, url);
      const content = await res.text();
      return { path: url, content, size: content.length, truncated: false };
    },
    staleTime: 30_000,
  });
}
```

`url` isn't restricted by the schema itself; same-origin-ness relies
on the dashboard's existing CSP blocking cross-origin fetches. This
view is meant for daemon-served doc routes (e.g. `/api/specs/...`),
not arbitrary external content.

### 7.3 Shared post-processing

Both hooks resolve to the same shape, so one `useMemo` pipeline runs
regardless of mode: raw content → gray-matter split → markdown sniff
(§8.2) → remark parse (shared by render + TOC extraction) → title
resolution.

## 8. Loading + error + edge cases

- **Loading** — shimmer title, shimmer meta-card outline, 4-5 shimmer
  body lines while the query is `isLoading`. No TOC skeleton (nothing
  to extract yet).
- **Not markdown** (§8.2) — if the path/url doesn't end in
  `.md`/`.markdown` and the first 2KB has no markdown heading syntax
  (`/^#{1,6}\s/m`), fall back to file-browser's non-markdown preview: a
  `<pre>` block, syntax-highlighted if the extension maps to a known
  language, else plain monospace. TOC and frontmatter parsing are both
  skipped.
- **Huge file** — `truncated: true` (local-file) or content over a
  500KB client-side cap (url mode, no server truncation contract for
  arbitrary same-origin routes) renders what's fetched plus a bottom
  banner: "Document truncated — showing first N KB. Copy path/url to
  view the rest locally." TOC still reflects the truncated content.
- **Empty file** — zero-length body after frontmatter strip renders a
  centered "This document is empty." No TOC; meta card still shows if
  frontmatter had content.
- **Fetch error** — 404 → "Spec not found at `<path/url>`" (Copy
  path/url stays available). Network/5xx → "Couldn't load this
  document." with a `Retry` button (`refetch()`). 403 (workspace not
  attached / access denied) → "You don't have access to this
  workspace," no retry.
- **No frontmatter, no h1** — title falls through to tier 3 (§5.4), no
  meta card. Expected steady state, not an error.

## 9. Agent-push examples

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Here's the shell spec →" \
    --pane-open '{"view": "spec-doc-reader", "args": {"workspaceId": "ws-vault", "path": "docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md"}}'
```

```
aq message send --to user --to-id dashboard --thread dashboard:demo \
    --body "The design doc for this feature is up →" \
    --pane-open '{"view": "spec-doc-reader", "args": {"workspaceId": "ws-project-repo-demo", "path": "docs/design/checkout-flow.md"}}'
```

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Full write-up here →" \
    --pane-open '{"view": "spec-doc-reader", "args": {"url": "/api/specs/2026-08-22-pane-plugin-interface-design.md"}}'
```

Per interface spec §6.5: arrival renders an inline "opened →" chip in
the transcript, and because `agent_pushable: true`, the client also
auto-dispatches `pane.open("spec-doc-reader", args)`, subject to the
frame's `to_kind === "user"` check.

## 10. Tests

`dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`:

**Manifest** — id matches directory name; `args_schema` accepts
`{workspaceId, path}` and `{url}`; rejects all-three, empty object,
`workspaceId`-only, and `path`-only; `open_shortcut` unset.

**Local-file mode** — loading skeleton while pending; renders title +
meta card + TOC + body once loaded; `close()` fires from the shell
close button; `Copy path/url` copies `path`; `Open in editor` present
and copies path + toast; `Open in editor` absent in url mode; 404
renders not-found state with `path` in the message.

**Url mode** — renders content from a mocked same-origin `fetch`;
`Copy path/url` copies the `url`; mocked network error renders the
generic error state with a working `Retry`.

**TOC** — extracts only `##`/`###`, excludes `#` and `####`+; TOC ids
match rendered heading ids exactly (slug parity against a fixture);
clicking an entry calls `scrollIntoView` without touching
`location.hash`; `t` focuses TOC, `Enter` on a focused entry triggers
the same scroll as a click.

**Frontmatter** — fenced YAML parses into the meta card (known fields
special-cased, unknown as plain rows); absent frontmatter → no card;
bold-label preamble fallback renders into the same card component.

**Edge cases** — non-markdown content renders `<pre>` fallback with no
TOC/meta card; `truncated: true` shows the truncation banner with a
partial TOC; empty post-frontmatter body renders the empty state.

## 11. Implementation checklist

- [ ] Create `dashboard/src/panes/spec-doc-reader/` directory.
- [ ] `manifest.ts` — id/name/description/icon, `args_schema` with
      `superRefine` mutual exclusion, `route_scope: "cross-route"`,
      `agent_pushable: true`, `palette_label: "Read spec"`,
      `palette_section: "Docs"`, no `open_shortcut`.
- [ ] `hooks.ts` — `useWorkspaceFile`, `useHostedDoc`, shared
      post-processing pipeline (gray-matter → markdown sniff → remark
      parse → TOC extraction → title resolution).
- [ ] `index.tsx` — `PaneViewProps` component: title resolution,
      frontmatter meta card (known fields + fallback rows + bold-label
      fallback), sticky/collapsible TOC sidebar with
      `IntersectionObserver` active highlighting, markdown body via
      `MarkdownPreview` + `@tailwindcss/typography`, `setToolbar` and
      `setShortcuts` wiring per §6.
- [ ] Loading skeleton; 404 / generic-error-with-retry / 403 states;
      not-markdown `<pre>` fallback; huge-file truncation banner;
      empty-body empty state.
- [ ] Add `spec-doc-reader` to `src/panes/registry.py` with
      `agent_pushable: true`.
- [ ] `__tests__/index.test.tsx` covering §10.
- [ ] Run the frontend/backend registry parity test.
- [ ] Confirm `gray-matter` and the TOC-slug dependency
      (`github-slugger`, already pulled in via `rehype-slug`) are
      present in `dashboard/package.json`; add only if missing.

## 12. Open questions

- **Full-page view mapping.** The brief suggests navigating to
  `/settings/playbooks/<id>` "or similar," but no route exists today
  for browsing arbitrary `docs/superpowers/specs/*.md` files
  full-page. This spec ships the button only for the concretely
  detectable playbook-spec case (§6.1); a general full-page spec
  reader route is a follow-up, possibly its own page rather than a
  pane concern.
- **Frontmatter convention drift.** This repo's specs use a bold-label
  preamble, not fenced YAML. The fallback heuristic (§5.3) is a
  pragmatic bridge, not a committed authoring convention — if the team
  standardizes on real frontmatter later, the fallback becomes dead
  code for new docs (harmless, but worth revisiting).
- **Sibling companion-spec links.** Companion-link resolution (§5.3)
  assumes siblings live in the same directory. No design yet for
  cross-directory companion links.
- **Same-origin URL allowlisting.** §7.2 relies on CSP to keep `url`
  same-origin; there's no explicit allowlist of which same-origin
  paths are legitimate "hosted spec" routes vs. an unrelated API
  endpoint that happens to return text. Low risk today; worth a second
  look if `url` mode grows beyond the daemon's own spec-serving route.
