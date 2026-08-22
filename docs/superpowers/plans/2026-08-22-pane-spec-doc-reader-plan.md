# Pane View: `spec-doc-reader` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `spec-doc-reader` pane view — a read-only, markdown-rendered
document reader (local workspace file or same-origin URL) with a sticky
table-of-contents sidebar and a frontmatter summary card — as a
self-contained directory under `dashboard/src/panes/spec-doc-reader/`, plus
its one-line server-side registry entry.

**Architecture:** A pane view is a directory of pure, independently-testable
pieces wired together by one component. `docProcessing.ts` holds the
side-effect-free markdown pipeline (frontmatter split, markdown sniff,
heading/TOC extraction, title resolution); `hooks.ts` holds the two React
Query data sources (workspace-file fetch, same-origin URL fetch) and the
`useMemo` glue that runs `docProcessing.ts` over whichever one resolved;
`index.tsx` is the `PaneViewProps` component that renders the shell-provided
layout, registers toolbar actions and shortcuts, and owns all
loading/error/edge-case UI. `manifest.ts` is the static declaration the
frontend pane registry auto-discovers via `import.meta.glob`.

**Tech Stack:** React 19, TanStack Query v5, `react-markdown` + `remark-gfm`
(existing `MarkdownPreview`), `rehype-slug` (new), `github-slugger` (new,
direct dep — see Task 3), `gray-matter` (new), `unified` + `remark-parse` +
`unist-util-visit` + `mdast-util-to-string` (new, for the standalone TOC
parse pass — see Task 4 rationale), `zod` (new — no pane view has shipped
into this repo yet, so this is the first manifest to need it), Vitest +
React Testing Library (assumed already configured by an earlier
pane/shell plan — see Prerequisites).

**Spec:** `docs/superpowers/specs/2026-08-22-pane-spec-doc-reader-design.md`
(this view's contract), depends on
`docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (pane
contract every view implements) and
`docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (shell
primitives, `<ShellPane>`, right-surface state machine). All three travel
with this plan; read them before starting.

## Global Constraints

- `route_scope` stays default (`"cross-route"`) — do not set it explicitly
  to `"route-scoped"`.
- Icons come **only** from `@heroicons/react/24/outline` — `lucide-react`
  must not be introduced anywhere in this view (see Prerequisites §"Spec
  bugs corrected by this plan" — the input spec's own manifest sample
  violates this and is corrected here).
- `open_shortcut` is omitted entirely when unset — never set to literal
  `null` (same correction).
- TOC entries: `##`/`###` headings only (`#` reserved for doc title,
  `####`+ excluded).
- TOC anchor navigation never touches `location.hash` or calls
  `history.pushState` — `scrollIntoView({ behavior: "smooth", block:
  "start" })` only.
- Non-markdown content gets a plain `<pre>` fallback — never a richer
  renderer.
- No frontmatter schema validation — permissive rendering of whatever YAML
  keys are present; unknown keys render as plain rows.
- `url` mode is not restricted by the zod schema itself; same-origin-ness
  is a CSP concern, not this view's — do not add same-origin allowlist
  logic here (per spec §12, that's a documented open question, not this
  plan's job).
- Per `dashboard/CLAUDE.md`: never call `fetch` directly for daemon
  *command* endpoints — go through the generated `@aq/ts-client` SDK or, for
  routes not modelled in the OpenAPI spec (this one isn't yet), the
  `legacy-fetch.ts` helpers. The one deliberate exception is `useHostedDoc`
  (Task 6) — it fetches an arbitrary same-origin doc route, not a daemon
  command, matching spec §7.2's own rationale for using raw `fetch`.

## Prerequisites

This view is v3 (Phase C, third wave) in the shell v2 rollout — per
`2026-08-22-dashboard-shell-v2-design.md` §10, it ships after Phase B (shell
foundation) and after the v1/v2 pane views. As of authoring this plan
(2026-08-22), **none of that infrastructure exists in this repo yet**:

- No `dashboard/src/panes/` directory, no `dashboard/src/panes/types.ts`
  (`PaneManifest`, `PaneViewProps`, `PaneToolbarAction`, `ShortcutBinding`),
  no `dashboard/src/panes/registry.ts`.
- No `dashboard/src/shell/ShellPane.tsx` / `useShellPane`.
- No `src/panes/registry.py` (backend mirror) and no
  `tests/test_pane_registry_parity.py`.
- No `GET /api/workspaces/{workspaceId}/file` endpoint (owned by the
  file-browser v1 plan).
- No `zod` dependency in `dashboard/package.json`.
- No test runner at all in `dashboard/` — `node_modules` has zero packages
  installed at authoring time, `package.json` has no `vitest` /
  `@testing-library/react` devDependency, and no `*.test.tsx` file exists
  anywhere under `dashboard/src`.

**Before starting Task 1**, verify these exist (they are owned by the
pane-plugin-interface plan, the shell-foundation plan, and the
task-detail/diff-review-changes/file-browser (v1) and
session-peek/console/playbook-run-inspector (v2) pane plans — all
upstream of this one per the phased rollout):

```bash
test -f dashboard/src/panes/types.ts && \
test -f dashboard/src/panes/registry.ts && \
test -f dashboard/src/shell/ShellPane.tsx && \
test -f src/panes/registry.py && \
grep -q '"vitest"' dashboard/package.json && \
grep -q '"zod"' dashboard/package.json && \
echo "prerequisites OK"
```

If any check fails, **stop** — do not improvise a substitute for shared
infrastructure inside this pane's own directory. Get the owning plan
finished first, then resume here. This plan assumes all of the above is in
place and working (its own test suite green) by the time Task 1 starts.

**Spec bugs corrected by this plan** (found while reading
`2026-08-22-pane-spec-doc-reader-design.md` §3 against
`2026-08-22-pane-plugin-interface-design.md` §4 — noted here so the
deviation is traceable, not silently "fixed"):

1. The input spec's own `manifest.ts` sample imports `BookOpenText` from
   `lucide-react`. The interface spec is explicit: "LucideIcon must NOT be
   introduced," heroicons only. This plan uses `BookOpenIcon` from
   `@heroicons/react/24/outline` instead.
2. The input spec's own `manifest.ts` sample sets `open_shortcut: null`.
   The interface spec is explicit: omit the field, never use a `null`
   literal ("a null literal violates the `string?` type"). This plan omits
   the field.

---

## File Structure

```
dashboard/src/panes/spec-doc-reader/
├── manifest.ts              # id/name/icon/args_schema/palette fields
├── docProcessing.ts         # pure: frontmatter split, markdown sniff,
│                             #   TOC extraction, title resolution
├── hooks.ts                 # useWorkspaceFile, useHostedDoc,
│                             #   useProcessedDoc (glue over docProcessing)
├── index.tsx                # PaneViewProps component
└── __tests__/
    ├── manifest.test.ts
    ├── docProcessing.test.ts
    └── index.test.tsx

dashboard/src/components/
├── MarkdownPreview.tsx       # MODIFY: add rehype-slug to plugin chain
└── __tests__/
    └── MarkdownPreview.test.tsx   # NEW

dashboard/package.json        # MODIFY: add zod, gray-matter, rehype-slug,
                               #   github-slugger, unified, remark-parse,
                               #   unist-util-visit, mdast-util-to-string
                               #   (only whichever are actually missing —
                               #   Task 1 checks first)

src/panes/registry.py         # MODIFY (or create if this is the first
                               #   pane plan to land) — one dict entry
tests/test_pane_registry_parity.py   # MODIFY (or create)
```

`docProcessing.ts` is split out from `hooks.ts`/`index.tsx` because it's
pure — no React, no network — and every function in it (frontmatter
parsing, TOC extraction, title resolution) has multiple documented edge
cases in the spec (§5.3, §5.4, §8.2) that are far cheaper to unit-test in
isolation than through a mounted component.

---

### Task 1: Pane directory skeleton + manifest

**Files:**
- Create: `dashboard/src/panes/spec-doc-reader/manifest.ts`
- Create: `dashboard/src/panes/spec-doc-reader/__tests__/manifest.test.ts`
- Modify: `dashboard/package.json` (add `zod` if missing — check first;
  per Prerequisites this may already be present from an earlier pane plan)

**Interfaces:**
- Consumes: `PaneManifest<TArgs>` from `dashboard/src/panes/types.ts`
  (Prerequisite; shape per `2026-08-22-pane-plugin-interface-design.md`
  §4).
- Produces: `manifest` (named export, `PaneManifest<SpecDocReaderArgs>`)
  and `SpecDocReaderArgs` (named export, `z.infer` type) — both consumed
  by `hooks.ts` (Task 6), `index.tsx` (throughout), and the frontend pane
  registry's `import.meta.glob` auto-discovery (no manual registry edit
  needed — per interface spec §4.1, dropping the directory in is enough).

- [ ] **Step 1: Confirm `zod` is available**

Run: `grep -n '"zod"' dashboard/package.json`

If absent, add it under `dependencies` in `dashboard/package.json`:

```json
    "zod": "^3.24.1",
```

Then run `npm install --prefix dashboard`.

- [ ] **Step 2: Write the failing manifest test**

Create `dashboard/src/panes/spec-doc-reader/__tests__/manifest.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { manifest, type SpecDocReaderArgs } from "../manifest";

describe("spec-doc-reader manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("spec-doc-reader");
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("registers a palette action under Docs", () => {
    expect(manifest.palette_label).toBe("Read spec");
    expect(manifest.palette_section).toBe("Docs");
  });

  it("is cross-route and agent-pushable", () => {
    expect(manifest.route_scope).toBe("cross-route");
    expect(manifest.agent_pushable).toBe(true);
  });

  describe("args_schema", () => {
    const parse = (v: unknown) => manifest.args_schema!.safeParse(v);

    it("accepts workspaceId + path", () => {
      const r = parse({ workspaceId: "ws-1", path: "docs/x.md" });
      expect(r.success).toBe(true);
    });

    it("accepts url alone", () => {
      const r = parse({ url: "/api/specs/x.md" });
      expect(r.success).toBe(true);
    });

    it("rejects an empty object", () => {
      expect(parse({}).success).toBe(false);
    });

    it("rejects workspaceId alone", () => {
      expect(parse({ workspaceId: "ws-1" }).success).toBe(false);
    });

    it("rejects path alone", () => {
      expect(parse({ path: "docs/x.md" }).success).toBe(false);
    });

    it("rejects all three present at once", () => {
      const r = parse({ workspaceId: "ws-1", path: "docs/x.md", url: "/api/x.md" });
      expect(r.success).toBe(false);
    });

    it("satisfies SpecDocReaderArgs typing on success", () => {
      const r = parse({ url: "/api/specs/x.md" });
      if (r.success) {
        const args: SpecDocReaderArgs = r.data;
        expect(args.url).toBe("/api/specs/x.md");
      }
    });
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/manifest.test.ts`

Expected: FAIL — `Cannot find module '../manifest'`.

- [ ] **Step 4: Write `manifest.ts`**

Create `dashboard/src/panes/spec-doc-reader/manifest.ts`:

```ts
import { z } from "zod";
import { BookOpenIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const argsSchema = z
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
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["path"],
        message: "path is required when workspaceId is set",
      });
    }
    if (val.path !== undefined && val.workspaceId === undefined) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["workspaceId"],
        message: "workspaceId is required when path is set",
      });
    }
  });

export type SpecDocReaderArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<SpecDocReaderArgs> = {
  id: "spec-doc-reader",
  name: "Spec Reader",
  description: "Read a spec or design doc with table of contents and frontmatter summary.",
  icon: BookOpenIcon,
  args_schema: argsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Read spec",
  palette_section: "Docs",
};
```

Note `open_shortcut` is not present at all (Global Constraints correction
#2) and `icon` is a heroicon, not `lucide-react` (correction #1).

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/manifest.test.ts`

Expected: PASS (all cases).

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/manifest.ts \
        dashboard/src/panes/spec-doc-reader/__tests__/manifest.test.ts \
        dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): spec-doc-reader pane — manifest + args schema"
```

---

### Task 2: Server-side registry entry

**Files:**
- Modify (or create — see Step 1): `src/panes/registry.py`
- Modify (or create): `tests/test_pane_registry_parity.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (backend, independent of the
  frontend build) — but the parity test does read
  `dashboard/src/panes/*/manifest.ts` off disk (Task 1's file must exist
  for this task's test to pass).
- Produces: `SERVER_PANE_REGISTRY["spec-doc-reader"]` — consumed by
  `_cmd_message_send`'s `--pane-open` validation (outside this plan's
  scope; that command already exists per the interface spec's assumption
  that some earlier pane plan wired it up).

- [ ] **Step 1: Check whether `src/panes/registry.py` already exists**

Run: `test -f src/panes/registry.py && echo exists || echo missing`

**If `missing`** (this is the first pane plan to land), create it:

```python
# src/panes/registry.py
"""Server-side mirror of the frontend pane view registry
(dashboard/src/panes/*/manifest.ts).

Used to validate `aq message send --pane-open` frames without giving the
daemon a JS runtime — see
docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7.
Kept in sync by hand; parity enforced by
tests/test_pane_registry_parity.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaneEntry:
    view_id: str
    agent_pushable: bool


SERVER_PANE_REGISTRY: dict[str, PaneEntry] = {
    "spec-doc-reader": PaneEntry(view_id="spec-doc-reader", agent_pushable=True),
}
```

**If `exists`**, add one entry via Edit (do not touch other views'
entries):

```python
    "spec-doc-reader": PaneEntry(view_id="spec-doc-reader", agent_pushable=True),
```

inserted alphabetically into the existing `SERVER_PANE_REGISTRY` dict
literal.

- [ ] **Step 2: Check whether the parity test already exists**

Run: `test -f tests/test_pane_registry_parity.py && echo exists || echo missing`

**If `missing`**, create it:

```python
# tests/test_pane_registry_parity.py
import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]
PANES_DIR = REPO_ROOT / "dashboard" / "src" / "panes"

_ID_RE = re.compile(r'^\s*id:\s*"([^"]+)"', re.MULTILINE)


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    for manifest_path in sorted(PANES_DIR.glob("*/manifest.ts")):
        text = manifest_path.read_text(encoding="utf-8")
        match = _ID_RE.search(text)
        assert match, f'no `id: "..."` found in {manifest_path}'
        ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids
```

**If `exists`**, leave it untouched — Task 1 already added
`spec-doc-reader/manifest.ts` on disk, which this test discovers
automatically.

- [ ] **Step 3: Run the parity test**

Run: `pytest tests/test_pane_registry_parity.py -v`

Expected: PASS — `spec-doc-reader` appears in both the frontend glob and
`SERVER_PANE_REGISTRY`. If it FAILS with a set-difference showing
`spec-doc-reader` missing from one side, re-check Step 1/Task 1 — both
sides must have it before this passes.

- [ ] **Step 4: Commit**

```bash
git add src/panes/registry.py tests/test_pane_registry_parity.py
git commit -m "feat(dashboard): spec-doc-reader — server-side pane registry entry"
```

---

### Task 3: `rehype-slug` dependency + wire into `MarkdownPreview`

**Files:**
- Modify: `dashboard/package.json` (add `rehype-slug`, `github-slugger` if
  missing)
- Modify: `dashboard/src/components/MarkdownPreview.tsx`
- Create: `dashboard/src/components/__tests__/MarkdownPreview.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces: every `MarkdownPreview`-rendered heading now carries a
  `github-slugger`-derived `id` attribute in the DOM. `docProcessing.ts`
  (Task 4) independently computes the same slugs for its TOC entries —
  parity between the two is what makes TOC anchor links resolve to the
  right DOM node; it is **not** achieved by AST sharing (react-markdown
  doesn't expose its internal AST to the host component), it's achieved by
  both sides using the same slugger package (`github-slugger`) over
  headings in the same document order (see Task 4 rationale note).

- [ ] **Step 1: Confirm dependencies**

Run: `grep -nE '"(rehype-slug|github-slugger)"' dashboard/package.json`

Add whichever is missing under `dependencies`:

```json
    "rehype-slug": "^6.0.0",
    "github-slugger": "^2.0.0",
```

`github-slugger` is added as a **direct** dependency even though
`rehype-slug` pulls it in transitively — Task 4's `docProcessing.ts`
imports it directly, and relying on hoisting for that import would be
fragile. Run `npm install --prefix dashboard`.

- [ ] **Step 2: Write the failing test**

Create `dashboard/src/components/__tests__/MarkdownPreview.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import MarkdownPreview from "../MarkdownPreview";

describe("MarkdownPreview", () => {
  it("assigns slug ids to headings", () => {
    const { container } = render(
      <MarkdownPreview source={"# Title\n\n## Goal\n\n### Sub Goal\n"} />,
    );
    const h2 = container.querySelector("h2");
    const h3 = container.querySelector("h3");
    expect(h2?.id).toBe("goal");
    expect(h3?.id).toBe("sub-goal");
  });

  it("dedupes repeated heading text the same way github-slugger does", () => {
    const { container } = render(
      <MarkdownPreview source={"## Overview\n\n## Overview\n"} />,
    );
    const headings = container.querySelectorAll("h2");
    expect(headings[0].id).toBe("overview");
    expect(headings[1].id).toBe("overview-1");
  });

  it("still renders GFM tables (existing behavior, unaffected)", () => {
    const { container } = render(
      <MarkdownPreview source={"| a | b |\n|---|---|\n| 1 | 2 |\n"} />,
    );
    expect(container.querySelector("table")).not.toBeNull();
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- components/__tests__/MarkdownPreview.test.tsx`

Expected: FAIL — headings render without `id` attributes.

- [ ] **Step 4: Wire `rehype-slug` into the plugin chain**

Edit `dashboard/src/components/MarkdownPreview.tsx`:

```tsx
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";

export interface MarkdownPreviewProps {
  source: string;
  className?: string;
}

export default function MarkdownPreview({ source, className }: MarkdownPreviewProps) {
  return (
    <div
      className={
        "prose prose-invert max-w-none prose-pre:bg-black/40 prose-code:text-indigo-300 " +
        (className ?? "")
      }
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSlug]}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
```

Only the import list and the `rehypePlugins` prop change; the rest of the
file (doc comment, prose classes) is unchanged.

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm run test --prefix dashboard -- components/__tests__/MarkdownPreview.test.tsx`

Expected: PASS.

- [ ] **Step 6: Full-suite regression check**

Run: `npm run test --prefix dashboard`

Expected: everything that previously passed still passes — `rehype-slug`
only adds `id` attributes, it doesn't change rendered text or table
structure. `TaskFilesPanel`'s existing markdown preview usage (rendered by
this same component) is unaffected.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/components/MarkdownPreview.tsx \
        dashboard/src/components/__tests__/MarkdownPreview.test.tsx \
        dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): wire rehype-slug into MarkdownPreview heading ids"
```

---

### Task 4: TOC extraction (`docProcessing.ts`) + sticky sidebar

**Files:**
- Create: `dashboard/src/panes/spec-doc-reader/docProcessing.ts`
- Create: `dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts`
- Modify: `dashboard/package.json` (add `unified`, `remark-parse`,
  `unist-util-visit`, `mdast-util-to-string` if missing)

**Interfaces:**
- Consumes: `github-slugger` (Task 3).
- Produces: `extractToc(markdownSource: string): TocEntry[]` with
  `interface TocEntry { id: string; text: string; depth: 2 | 3 }` —
  consumed by `hooks.ts` (Task 6) and rendered by `index.tsx`'s TOC
  sidebar (this task, Step 6 below) and toolbar/shortcuts (Tasks 7–8).

**Design note on "same AST" (spec §5.2):** the input spec says TOC
extraction is "derived once per document load from the same markdown AST
`MarkdownPreview` already builds ... not reimplemented, reused." In
practice `react-markdown` does not expose its internal `remark`/`rehype`
AST to the host component — there is no hook for "give me the parsed tree
after render." `extractToc` therefore runs its own `remark-parse` pass
over the same markdown source string. Parity between its ids and the
rendered heading ids is guaranteed a different way: both sides slug
**every** heading (all depths, `h1`–`h6`) in document order using the same
`github-slugger` package, so duplicate-text dedup counters (`-1`, `-2`,
...) stay in lockstep — `extractToc` only *returns* depth-2/3 entries, but
it must still feed depth-1 and depth-4+ headings through the slugger so
the counter doesn't drift. This is implemented below and covered by Step
2's duplicate-across-depths test.

- [ ] **Step 1: Confirm dependencies**

Run: `grep -nE '"(unified|remark-parse|unist-util-visit|mdast-util-to-string)"' dashboard/package.json`

Add whichever are missing:

```json
    "unified": "^11.0.5",
    "remark-parse": "^11.0.0",
    "unist-util-visit": "^5.0.0",
    "mdast-util-to-string": "^4.0.0",
```

Run `npm install --prefix dashboard`.

- [ ] **Step 2: Write the failing test**

Create `dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts`
(this step only covers `extractToc` — later tasks append `describe`
blocks to this same file for the other `docProcessing.ts` exports):

```ts
import { describe, expect, it } from "vitest";
import { extractToc } from "../docProcessing";

describe("extractToc", () => {
  it("extracts only ## and ### headings", () => {
    const md = [
      "# Title",
      "## Goal",
      "### Sub Goal",
      "#### Too Deep",
      "## Non-goals",
    ].join("\n\n");
    const toc = extractToc(md);
    expect(toc.map((e) => e.text)).toEqual(["Goal", "Sub Goal", "Non-goals"]);
    expect(toc.map((e) => e.depth)).toEqual([2, 3, 2]);
  });

  it("produces ids matching github-slugger's default algorithm", () => {
    const toc = extractToc("## Hello World\n");
    expect(toc[0].id).toBe("hello-world");
  });

  it("keeps dedup counters in sync with h1/h4+ headings in between (slug parity)", () => {
    // "Overview" appears as h1, then h2, then h4 — a slugger that only
    // sees the h2/h3 subset would slug both non-h1 occurrences as
    // "overview" (since it never saw the h1 consume "overview" first).
    // The real MarkdownPreview render slugs ALL headings in document
    // order, so the h2 here must come out as "overview-1", matching what
    // rehype-slug would assign to the same h2 node.
    const md = "# Overview\n\n## Overview\n\n#### Overview\n";
    const toc = extractToc(md);
    expect(toc).toEqual([{ id: "overview-1", text: "Overview", depth: 2 }]);
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/docProcessing.test.ts`

Expected: FAIL — `Cannot find module '../docProcessing'`.

- [ ] **Step 4: Implement `extractToc` (and the `TocEntry` type)**

Create `dashboard/src/panes/spec-doc-reader/docProcessing.ts`:

```ts
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";
import { toString as mdastToString } from "mdast-util-to-string";
import GithubSlugger from "github-slugger";
import type { Root, Heading } from "mdast";

export interface TocEntry {
  id: string;
  text: string;
  depth: 2 | 3;
}

/**
 * Parse `markdownSource` and return TOC entries for ## and ### headings
 * only. Slugs every heading (all depths) in document order through one
 * github-slugger instance so dedup counters match what rehype-slug
 * assigns the same headings during MarkdownPreview's render — see the
 * design note in this file's plan task for why this can't just reuse
 * react-markdown's internal AST.
 */
export function extractToc(markdownSource: string): TocEntry[] {
  const tree = unified().use(remarkParse).use(remarkGfm).parse(markdownSource) as Root;
  const slugger = new GithubSlugger();
  const entries: TocEntry[] = [];

  visit(tree, "heading", (node: Heading) => {
    const text = mdastToString(node);
    const id = slugger.slug(text);
    if (node.depth === 2 || node.depth === 3) {
      entries.push({ id, text, depth: node.depth });
    }
  });

  return entries;
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/docProcessing.test.ts`

Expected: PASS (all three cases, including the dedup-parity case).

- [ ] **Step 6: TOC sidebar — write the failing component test**

This step needs `index.tsx` to exist with at least a stub that consumes
`extractToc`, so it's written together with a minimal component. Create
`dashboard/src/panes/spec-doc-reader/index.tsx` with **only** the TOC
sidebar piece for now (Tasks 5–9 fill in the rest of the same file):

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneViewProps } from "../types";
import type { SpecDocReaderArgs } from "./manifest";
import { extractToc, type TocEntry } from "./docProcessing";

const TOC_BREAKPOINT_PX = 420;

function useContainerWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (typeof w === "number") setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

function TocList({
  toc,
  activeId,
  onSelect,
  tocRef,
}: {
  toc: TocEntry[];
  activeId: string | null;
  onSelect: (id: string) => void;
  tocRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <nav ref={tocRef} aria-label="Table of contents" className="text-xs">
      <ul className="space-y-0.5">
        {toc.map((entry) => (
          <li key={entry.id} className={entry.depth === 3 ? "pl-3" : ""}>
            <button
              type="button"
              data-toc-id={entry.id}
              onClick={() => onSelect(entry.id)}
              className={
                "block w-full truncate rounded px-2 py-1 text-left hover:bg-gray-800 " +
                (activeId === entry.id ? "bg-indigo-950/60 text-indigo-200" : "text-gray-400")
              }
            >
              {entry.text}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export default function SpecDocReaderPane({ args }: PaneViewProps<SpecDocReaderArgs>) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const tocRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const width = useContainerWidth(containerRef);
  const [activeId, setActiveId] = useState<string | null>(null);

  // Placeholder markdown source until Task 6 wires real data — replaced
  // in that task's edit to this same function.
  const markdownSource = "";
  const toc = useMemo(() => extractToc(markdownSource), [markdownSource]);

  useEffect(() => {
    const scrollEl = bodyRef.current;
    if (!scrollEl || toc.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting);
        if (visible.length > 0) {
          const topMost = visible.reduce((a, b) => (a.boundingClientRect.top < b.boundingClientRect.top ? a : b));
          setActiveId(topMost.target.id);
        }
      },
      { root: scrollEl, threshold: 0.1 },
    );
    for (const entry of toc) {
      const el = scrollEl.querySelector(`#${CSS.escape(entry.id)}`);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [toc]);

  const scrollToHeading = (id: string) => {
    const el = bodyRef.current?.querySelector(`#${CSS.escape(id)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setActiveId(id);
  };

  const tocElement = toc.length > 0 && (
    <TocList toc={toc} activeId={activeId} onSelect={scrollToHeading} tocRef={tocRef} />
  );

  return (
    <div ref={containerRef} className="flex h-full flex-col overflow-hidden">
      {width > 0 && width < TOC_BREAKPOINT_PX ? (
        tocElement && (
          <details className="border-b border-gray-800 px-3 py-2">
            <summary className="cursor-pointer text-xs text-gray-400">Table of contents</summary>
            <div className="mt-2">{tocElement}</div>
          </details>
        )
      ) : (
        <div className="flex flex-1 overflow-hidden">
          {tocElement && (
            <aside className="w-[180px] shrink-0 overflow-y-auto border-r border-gray-800 p-2">
              {tocElement}
            </aside>
          )}
          <div ref={bodyRef} className="flex-1 overflow-y-auto p-4" />
        </div>
      )}
    </div>
  );
}
```

And a starter `index.test.tsx` covering just the TOC pieces (Tasks 5–9
append more `describe` blocks to this same file):

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import SpecDocReaderPane from "../index";
import type { PaneViewProps } from "../../types";
import type { SpecDocReaderArgs } from "../manifest";

function baseProps(args: SpecDocReaderArgs): PaneViewProps<SpecDocReaderArgs> {
  return {
    args,
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

describe("SpecDocReaderPane — TOC", () => {
  it("renders without crashing given valid args", () => {
    render(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
  });
});
```

Note the placeholder `markdownSource = ""` yields an empty TOC — this
"renders without crashing" test passes trivially. Task 6 replaces the
placeholder with real data from `useProcessedDoc`, and Task 4's remaining
TOC-interaction tests (slug parity against a rendered fixture, click
triggers `scrollIntoView`, no `location.hash` mutation) are added in Task
8 once the body actually renders real markdown and shortcut wiring exists
to drive `t` / `Enter`. This ordering avoids writing TOC-click tests
against a body that has no headings to click through to yet.

- [ ] **Step 7: Run it to verify it passes**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__`

Expected: PASS — both `docProcessing.test.ts` and the new
`index.test.tsx` smoke test.

- [ ] **Step 8: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/docProcessing.ts \
        dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx \
        dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): spec-doc-reader — TOC extraction + sticky/collapsible sidebar"
```

---

### Task 5: Frontmatter meta card

**Files:**
- Modify: `dashboard/src/panes/spec-doc-reader/docProcessing.ts`
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts`
- Modify: `dashboard/src/panes/spec-doc-reader/index.tsx`
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`
- Modify: `dashboard/package.json` (add `gray-matter` if missing)

**Interfaces:**
- Consumes: nothing new from earlier tasks besides `docProcessing.ts`'s
  module (adds exports to the same file).
- Produces: `parseFrontmatter(raw: string): { data: Record<string,
  unknown> | null; content: string }` and `resolveTitle(opts: {
  frontmatter: Record<string, unknown> | null; body: string; fallbackName:
  string }): string` — both consumed by `hooks.ts` (Task 6).

- [ ] **Step 1: Confirm `gray-matter` dependency**

Run: `grep -n '"gray-matter"' dashboard/package.json`

Add if missing:

```json
    "gray-matter": "^4.0.3",
```

Run `npm install --prefix dashboard`.

- [ ] **Step 2: Write the failing tests**

Append to `dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts`:

```ts
import { parseFrontmatter, resolveTitle } from "../docProcessing";

describe("parseFrontmatter", () => {
  it("parses a fenced YAML block", () => {
    const raw = "---\nstatus: design\ndate: 2026-08-22\n---\n\n## Body\n";
    const { data, content } = parseFrontmatter(raw);
    expect(data).toEqual({ status: "design", date: "2026-08-22" });
    expect(content.trim()).toBe("## Body");
  });

  it("returns null data when there is no frontmatter of either kind", () => {
    const { data, content } = parseFrontmatter("## Just a body\n");
    expect(data).toBeNull();
    expect(content).toBe("## Just a body\n");
  });

  it("falls back to bold-label preamble parsing when no fenced block exists", () => {
    const raw = [
      "# Title",
      "",
      "**Status:** design (approved).",
      "**Depends on:** other-spec.md",
      "",
      "## Body",
    ].join("\n");
    const { data } = parseFrontmatter(raw);
    expect(data).toEqual({
      Status: "design (approved).",
      "Depends on": "other-spec.md",
    });
  });

  it("only scans the preamble before the first ## heading for the bold-label fallback", () => {
    const raw = [
      "# Title",
      "**Status:** design",
      "",
      "## Body",
      "**Not:** a frontmatter field",
    ].join("\n");
    const { data } = parseFrontmatter(raw);
    expect(data).toEqual({ Status: "design" });
  });
});

describe("resolveTitle", () => {
  it("prefers frontmatter title over name", () => {
    const t = resolveTitle({
      frontmatter: { title: "Real Title", name: "Other" },
      body: "# H1 Title\n",
      fallbackName: "fallback.md",
    });
    expect(t).toBe("Real Title");
  });

  it("falls back to name when title is absent", () => {
    const t = resolveTitle({
      frontmatter: { name: "Named Doc" },
      body: "# H1 Title\n",
      fallbackName: "fallback.md",
    });
    expect(t).toBe("Named Doc");
  });

  it("falls back to the first h1 when there is no frontmatter title/name", () => {
    const t = resolveTitle({ frontmatter: null, body: "# H1 Title\n\nbody", fallbackName: "fallback.md" });
    expect(t).toBe("H1 Title");
  });

  it("falls back to a humanized filename when there is no frontmatter or h1", () => {
    const t = resolveTitle({ frontmatter: null, body: "no heading here", fallbackName: "my-spec-doc.md" });
    expect(t).toBe("My Spec Doc");
  });

  it("humanizes the last path segment of a URL fallback", () => {
    const t = resolveTitle({ frontmatter: null, body: "", fallbackName: "/api/specs/checkout_flow.md" });
    expect(t).toBe("Checkout Flow");
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/docProcessing.test.ts`

Expected: FAIL — `parseFrontmatter`/`resolveTitle` not exported.

- [ ] **Step 4: Implement `parseFrontmatter` and `resolveTitle`**

Append to `dashboard/src/panes/spec-doc-reader/docProcessing.ts`:

```ts
import matter from "gray-matter";

const BOLD_LABEL_LINE = /^\*\*([^*]+):\*\*\s*(.+)$/;

/**
 * Split frontmatter from body. Prefers a fenced ---/--- YAML block
 * (gray-matter). When none is present, falls back to a heuristic scan of
 * the preamble (everything before the first `##` heading) for
 * `**Label:** value` lines — this repo's own specs use that convention
 * instead of fenced YAML (see spec §5.3 / §12).
 */
export function parseFrontmatter(raw: string): {
  data: Record<string, unknown> | null;
  content: string;
} {
  const { data, content } = matter(raw);
  if (data && Object.keys(data).length > 0) {
    return { data, content };
  }

  const firstH2 = content.search(/^##\s/m);
  const preamble = firstH2 === -1 ? content : content.slice(0, firstH2);
  const fallback: Record<string, string> = {};
  for (const line of preamble.split("\n")) {
    const m = BOLD_LABEL_LINE.exec(line.trim());
    if (m) fallback[m[1].trim()] = m[2].trim();
  }
  return { data: Object.keys(fallback).length > 0 ? fallback : null, content };
}

function humanize(nameOrPath: string): string {
  const base = nameOrPath.split("/").pop() ?? nameOrPath;
  const noExt = base.replace(/\.(md|markdown)$/i, "");
  return noExt
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Title resolution order: frontmatter `title`, then `name`, then the
 * body's first h1, then a humanized filename/URL-segment fallback.
 */
export function resolveTitle(opts: {
  frontmatter: Record<string, unknown> | null;
  body: string;
  fallbackName: string;
}): string {
  const fm = opts.frontmatter;
  if (fm) {
    if (typeof fm.title === "string" && fm.title.trim()) return fm.title.trim();
    if (typeof fm.name === "string" && fm.name.trim()) return fm.name.trim();
  }
  const h1 = /^#\s+(.+)$/m.exec(opts.body);
  if (h1) return h1[1].trim();
  return humanize(opts.fallbackName);
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/docProcessing.test.ts`

Expected: PASS.

- [ ] **Step 6: Meta card component — write the failing test**

Append to `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`:

```tsx
describe("SpecDocReaderPane — meta card", () => {
  it("renders known fields (status pill, date) and unknown fields as plain rows", () => {
    render(
      <SpecDocReaderPane
        {...baseProps({ url: "/api/specs/x.md" })}
      />,
    );
    // Meta card rendering itself is exercised end-to-end once Task 6
    // wires real data through useProcessedDoc — this placeholder asserts
    // the MetaCard component renders the right shape in isolation.
  });
});

import { MetaCard } from "../index";

describe("MetaCard", () => {
  it("renders status as a pill, date as-is, companions as buttons, and unknown keys as rows", () => {
    render(
      <MetaCard
        data={{ status: "design", date: "2026-08-22", companions: "other.md", extra: "value" }}
        onCompanionClick={vi.fn()}
      />,
    );
    expect(screen.getByText("design")).toBeInTheDocument();
    expect(screen.getByText("2026-08-22")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "other.md" })).toBeInTheDocument();
    expect(screen.getByText("extra")).toBeInTheDocument();
    expect(screen.getByText("value")).toBeInTheDocument();
  });

  it("renders nothing when data is null", () => {
    const { container } = render(<MetaCard data={null} onCompanionClick={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 7: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: FAIL — `MetaCard` not exported from `../index`.

- [ ] **Step 8: Implement `MetaCard` and wire it into the pane**

Edit `dashboard/src/panes/spec-doc-reader/index.tsx` — add the `MetaCard`
export and render it above the TOC/body split:

```tsx
const KNOWN_KEYS = new Set(["status", "date", "companion_specs", "companions"]);

function companionList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") return value.split(",").map((s) => s.trim()).filter(Boolean);
  return [];
}

export function MetaCard({
  data,
  onCompanionClick,
}: {
  data: Record<string, unknown> | null;
  onCompanionClick: (companion: string) => void;
}) {
  if (!data || Object.keys(data).length === 0) return null;

  const companions = companionList(data.companion_specs ?? data.companions);
  const unknownEntries = Object.entries(data).filter(([k]) => !KNOWN_KEYS.has(k));

  return (
    <div className="mb-4 rounded border border-gray-800 bg-gray-950 p-3 text-xs">
      {typeof data.status === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Status:</span>
          <span className="rounded-full bg-indigo-950/60 px-2 py-0.5 text-indigo-200">
            {data.status}
          </span>
        </div>
      )}
      {typeof data.date === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Date:</span>
          <span className="text-gray-300">{data.date}</span>
        </div>
      )}
      {companions.length > 0 && (
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <span className="text-gray-500">Companions:</span>
          {companions.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => onCompanionClick(c)}
              className="text-indigo-300 underline hover:text-indigo-200"
            >
              {c}
            </button>
          ))}
        </div>
      )}
      {unknownEntries.map(([key, value]) => (
        <div key={key} className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">{key}:</span>
          <span className="text-gray-300">{String(value)}</span>
        </div>
      ))}
    </div>
  );
}
```

Wire `<MetaCard>` into the top of the two-column layout (this edit
threads it into the JSX built in Task 4; the full assembled `index.tsx`
is shown in Task 6's Step 4 once real data flows through it, since
rendering `MetaCard` with real `frontmatter` data requires the
`useProcessedDoc` hook from that task — for now `MetaCard` is exported
and independently tested, not yet mounted with live data).

- [ ] **Step 9: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/docProcessing.ts \
        dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/panes/spec-doc-reader/__tests__/docProcessing.test.ts \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx \
        dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): spec-doc-reader — frontmatter meta card (fenced YAML + bold-label fallback)"
```

---

### Task 6: Data — workspace-file endpoint + same-origin URL fetch

**Files:**
- Create: `dashboard/src/panes/spec-doc-reader/hooks.ts`
- Modify: `dashboard/src/panes/spec-doc-reader/index.tsx` (wire real data
  through the TOC sidebar + meta card from Tasks 4–5)
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `apiGet` from `dashboard/src/api/legacy-fetch.ts` (existing —
  see `dashboard/src/api/taskFiles.ts` for the established pattern this
  follows); `parseFrontmatter`, `extractToc`, `resolveTitle` from
  `docProcessing.ts` (Tasks 4–5).
- Produces: `useWorkspaceFile(workspaceId, path)`, `useHostedDoc(url)`,
  `useProcessedDoc(args, fileQuery)` returning `ProcessedDoc | null` —
  consumed by `index.tsx`'s main render and by Tasks 7–9.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`
(this exercises the whole pipeline through the component, since
`hooks.ts` is thin React Query glue over already-tested
`docProcessing.ts` functions — no separate `hooks.test.ts`):

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("SpecDocReaderPane — local-file mode", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/api/workspaces/")) {
          return new Response(
            JSON.stringify({
              path: "docs/x.md",
              content: "---\nstatus: design\n---\n\n# X\n\n## Goal\n\nbody\n",
              size: 40,
              truncated: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("not found", { status: 404 });
      }),
    );
  });

  it("renders title + meta card + TOC + body once loaded", async () => {
    renderWithQuery(
      <SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/x.md" })} />,
    );
    expect(await screen.findByText("X")).toBeInTheDocument();
    expect(await screen.findByText("design")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Goal" })).toBeInTheDocument();
    expect(await screen.findByText("body")).toBeInTheDocument();
  });
});

describe("SpecDocReaderPane — url mode", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("# Hosted Doc\n\nhello\n", { status: 200 })),
    );
  });

  it("renders content from a mocked same-origin fetch", async () => {
    renderWithQuery(
      <SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />,
    );
    expect(await screen.findByText("Hosted Doc")).toBeInTheDocument();
    expect(await screen.findByText("hello")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: FAIL — pane still renders the empty placeholder from Task 4.

- [ ] **Step 3: Implement `hooks.ts`**

Create `dashboard/src/panes/spec-doc-reader/hooks.ts`:

```ts
import { useMemo } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { apiGet } from "../../api/legacy-fetch";
import { extractToc, parseFrontmatter, resolveTitle, type TocEntry } from "./docProcessing";
import type { SpecDocReaderArgs } from "./manifest";

export interface WorkspaceFileResponse {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
}

const HOSTED_DOC_CAP_BYTES = 500 * 1024;

export class HostedDocFetchError extends Error {
  status: number;
  url: string;
  constructor(status: number, url: string) {
    super(`hosted doc fetch failed: ${status} ${url}`);
    this.status = status;
    this.url = url;
  }
}

export function useWorkspaceFile(workspaceId: string, path: string) {
  return useQuery({
    queryKey: ["workspace-file", workspaceId, path],
    queryFn: () =>
      apiGet<WorkspaceFileResponse>(
        `/api/workspaces/${encodeURIComponent(workspaceId)}/file?path=${encodeURIComponent(path)}`,
      ),
    staleTime: 30_000,
  });
}

// Deliberate exception to "never call fetch directly for daemon endpoints"
// (dashboard/CLAUDE.md): this isn't a daemon command, it's an arbitrary
// same-origin doc route with no SDK binding — see spec §7.2.
export function useHostedDoc(url: string) {
  return useQuery({
    queryKey: ["hosted-doc", url],
    queryFn: async (): Promise<WorkspaceFileResponse> => {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new HostedDocFetchError(res.status, url);
      const full = await res.text();
      const truncated = full.length > HOSTED_DOC_CAP_BYTES;
      const content = truncated ? full.slice(0, HOSTED_DOC_CAP_BYTES) : full;
      return { path: url, content, size: full.length, truncated };
    },
    staleTime: 30_000,
  });
}

const MARKDOWN_EXT = /\.(md|markdown)$/i;
const HEADING_LINE = /^#{1,6}\s/m;

function looksLikeMarkdown(pathOrUrl: string, content: string): boolean {
  if (MARKDOWN_EXT.test(pathOrUrl)) return true;
  return HEADING_LINE.test(content.slice(0, 2048));
}

export interface ProcessedDoc {
  isMarkdown: boolean;
  frontmatter: Record<string, unknown> | null;
  body: string;
  title: string;
  toc: TocEntry[];
  truncated: boolean;
  rawContent: string;
}

/**
 * Shared post-processing pipeline (spec §7.3): whichever data hook
 * resolved (local-file or url), run frontmatter split → markdown sniff →
 * TOC extraction → title resolution once via useMemo.
 */
export function useProcessedDoc(
  args: SpecDocReaderArgs,
  fileQuery: UseQueryResult<WorkspaceFileResponse>,
): ProcessedDoc | null {
  const data = fileQuery.data;
  return useMemo(() => {
    if (!data) return null;
    const pathOrUrl = args.path ?? args.url ?? data.path;
    const isMarkdown = looksLikeMarkdown(pathOrUrl, data.content);

    if (!isMarkdown) {
      return {
        isMarkdown: false,
        frontmatter: null,
        body: data.content,
        title: resolveTitle({ frontmatter: null, body: "", fallbackName: pathOrUrl }),
        toc: [],
        truncated: data.truncated,
        rawContent: data.content,
      };
    }

    const { data: frontmatter, content: body } = parseFrontmatter(data.content);
    const title = resolveTitle({ frontmatter, body, fallbackName: pathOrUrl });
    const toc = extractToc(body);

    return {
      isMarkdown: true,
      frontmatter,
      body,
      title,
      toc,
      truncated: data.truncated,
      rawContent: data.content,
    };
  }, [data, args.path, args.url]);
}
```

- [ ] **Step 4: Wire real data through `index.tsx`**

Replace the placeholder body of `dashboard/src/panes/spec-doc-reader/index.tsx`
from Task 4 with the full component (this supersedes the Task 4 version
in full — shown complete here since the placeholder's `markdownSource`
line and the unmounted `MetaCard` from Task 5 both change):

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneViewProps } from "../types";
import type { SpecDocReaderArgs } from "./manifest";
import { extractToc, type TocEntry } from "./docProcessing";
import { useWorkspaceFile, useHostedDoc, useProcessedDoc } from "./hooks";
import MarkdownPreview from "../../components/MarkdownPreview";

const TOC_BREAKPOINT_PX = 420;

function useContainerWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (typeof w === "number") setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

function TocList({
  toc,
  activeId,
  onSelect,
  tocRef,
}: {
  toc: TocEntry[];
  activeId: string | null;
  onSelect: (id: string) => void;
  tocRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <nav ref={tocRef} aria-label="Table of contents" className="text-xs">
      <ul className="space-y-0.5">
        {toc.map((entry) => (
          <li key={entry.id} className={entry.depth === 3 ? "pl-3" : ""}>
            <button
              type="button"
              data-toc-id={entry.id}
              onClick={() => onSelect(entry.id)}
              className={
                "block w-full truncate rounded px-2 py-1 text-left hover:bg-gray-800 " +
                (activeId === entry.id ? "bg-indigo-950/60 text-indigo-200" : "text-gray-400")
              }
            >
              {entry.text}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

const KNOWN_KEYS = new Set(["status", "date", "companion_specs", "companions"]);

function companionList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") return value.split(",").map((s) => s.trim()).filter(Boolean);
  return [];
}

export function MetaCard({
  data,
  onCompanionClick,
}: {
  data: Record<string, unknown> | null;
  onCompanionClick: (companion: string) => void;
}) {
  if (!data || Object.keys(data).length === 0) return null;
  const companions = companionList(data.companion_specs ?? data.companions);
  const unknownEntries = Object.entries(data).filter(([k]) => !KNOWN_KEYS.has(k));

  return (
    <div className="mb-4 rounded border border-gray-800 bg-gray-950 p-3 text-xs">
      {typeof data.status === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Status:</span>
          <span className="rounded-full bg-indigo-950/60 px-2 py-0.5 text-indigo-200">{data.status}</span>
        </div>
      )}
      {typeof data.date === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Date:</span>
          <span className="text-gray-300">{data.date}</span>
        </div>
      )}
      {companions.length > 0 && (
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <span className="text-gray-500">Companions:</span>
          {companions.map((c) => (
            <button key={c} type="button" onClick={() => onCompanionClick(c)} className="text-indigo-300 underline hover:text-indigo-200">
              {c}
            </button>
          ))}
        </div>
      )}
      {unknownEntries.map(([key, value]) => (
        <div key={key} className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">{key}:</span>
          <span className="text-gray-300">{String(value)}</span>
        </div>
      ))}
    </div>
  );
}

function siblingPath(currentPath: string, companionFile: string): string {
  const idx = currentPath.lastIndexOf("/");
  const dir = idx === -1 ? "" : currentPath.slice(0, idx + 1);
  return dir + companionFile;
}

export default function SpecDocReaderPane({ args, setArgs }: PaneViewProps<SpecDocReaderArgs>) {
  const isLocalFile = args.workspaceId !== undefined && args.path !== undefined;

  const localQuery = useWorkspaceFile(args.workspaceId ?? "", args.path ?? "");
  const hostedQuery = useHostedDoc(args.url ?? "");
  const fileQuery = isLocalFile ? localQuery : hostedQuery;

  const doc = useProcessedDoc(args, fileQuery);

  const bodyRef = useRef<HTMLDivElement>(null);
  const tocRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const width = useContainerWidth(containerRef);
  const [activeId, setActiveId] = useState<string | null>(null);

  const toc = doc?.toc ?? [];

  useEffect(() => {
    const scrollEl = bodyRef.current;
    if (!scrollEl || toc.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting);
        if (visible.length > 0) {
          const topMost = visible.reduce((a, b) => (a.boundingClientRect.top < b.boundingClientRect.top ? a : b));
          setActiveId(topMost.target.id);
        }
      },
      { root: scrollEl, threshold: 0.1 },
    );
    for (const entry of toc) {
      const el = scrollEl.querySelector(`#${CSS.escape(entry.id)}`);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [toc]);

  const scrollToHeading = (id: string) => {
    const el = bodyRef.current?.querySelector(`#${CSS.escape(id)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setActiveId(id);
  };

  const handleCompanionClick = (companion: string) => {
    if (isLocalFile && args.path) {
      setArgs({ workspaceId: args.workspaceId, path: siblingPath(args.path, companion) } as SpecDocReaderArgs);
    }
    // url mode: no cross-directory companion resolution yet (spec §12).
  };

  const tocElement = toc.length > 0 && (
    <TocList toc={toc} activeId={activeId} onSelect={scrollToHeading} tocRef={tocRef} />
  );

  if (fileQuery.isLoading) {
    return (
      <div className="p-4">
        <div className="mb-2 h-5 w-2/3 animate-pulse rounded bg-gray-800" />
        <div className="mb-4 h-16 w-full animate-pulse rounded bg-gray-900" />
        {[...Array(5)].map((_, i) => (
          <div key={i} className="mb-2 h-3 w-full animate-pulse rounded bg-gray-900" />
        ))}
      </div>
    );
  }

  if (fileQuery.isError) {
    const err = fileQuery.error as { message?: string; status?: number } | undefined;
    const pathOrUrl = args.path ?? args.url ?? "";
    const status = (err as { status?: number })?.status;
    if (status === 404) {
      return <div className="p-4 text-sm text-gray-400">Spec not found at <code>{pathOrUrl}</code></div>;
    }
    if (status === 403) {
      return <div className="p-4 text-sm text-red-400">You don&apos;t have access to this workspace.</div>;
    }
    return (
      <div className="p-4 text-sm text-red-400">
        Couldn&apos;t load this document.
        <button type="button" onClick={() => fileQuery.refetch()} className="ml-2 rounded border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-800">
          Retry
        </button>
      </div>
    );
  }

  if (!doc) return null;

  if (!doc.isMarkdown) {
    return (
      <pre className="h-full overflow-auto whitespace-pre-wrap p-4 font-mono text-xs text-gray-200">{doc.rawContent}</pre>
    );
  }

  if (doc.body.trim().length === 0) {
    return <div className="flex h-full items-center justify-center text-sm text-gray-500">This document is empty.</div>;
  }

  return (
    <div ref={containerRef} className="flex h-full flex-col overflow-hidden">
      <div className="border-b border-gray-800 p-4">
        <h1 className="mb-2 text-lg font-semibold text-gray-100">{doc.title}</h1>
        <MetaCard data={doc.frontmatter} onCompanionClick={handleCompanionClick} />
      </div>
      {doc.truncated && (
        <div className="border-b border-amber-900 bg-amber-950/40 px-4 py-2 text-xs text-amber-300">
          Document truncated — showing first {Math.round((fileQuery.data?.size ?? 0) / 1024)} KB. Copy path/url to
          view the rest locally.
        </div>
      )}
      {width > 0 && width < TOC_BREAKPOINT_PX ? (
        <>
          {tocElement && (
            <details className="border-b border-gray-800 px-3 py-2">
              <summary className="cursor-pointer text-xs text-gray-400">Table of contents</summary>
              <div className="mt-2">{tocElement}</div>
            </details>
          )}
          <div ref={bodyRef} className="flex-1 overflow-y-auto p-4">
            <MarkdownPreview source={doc.body} />
          </div>
        </>
      ) : (
        <div className="flex flex-1 overflow-hidden">
          {tocElement && (
            <aside className="w-[180px] shrink-0 overflow-y-auto border-r border-gray-800 p-2">{tocElement}</aside>
          )}
          <div ref={bodyRef} className="flex-1 overflow-y-auto p-4">
            <MarkdownPreview source={doc.body} />
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: PASS — local-file mode and url mode both render title, meta
card, TOC, and body from mocked data.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/hooks.ts \
        dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx
git commit -m "feat(dashboard): spec-doc-reader — wire workspace-file + hosted-doc data through the pipeline"
```

---

### Task 7: Toolbar

**Files:**
- Modify: `dashboard/src/panes/spec-doc-reader/index.tsx`
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `setToolbar` from `PaneViewProps` (interface spec §5); `close`
  is already destructured but unused until now.
- Produces: nothing new consumed by later tasks — toolbar is leaf UI.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`:

```tsx
describe("SpecDocReaderPane — toolbar", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("# Doc\n\nbody\n", { status: 200 })),
    );
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("registers Copy URL + no Open-in-editor in url mode", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    expect(props.setToolbar).toHaveBeenCalled();
    const actions = (props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    expect(actions.map((a: { label: string }) => a.label)).toEqual(["Copy URL"]);
  });

  it("Copy path/url copies the url in url mode", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = (props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    actions[0].onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("/api/specs/x.md");
  });

  it("registers Copy path + Open in editor in local-file mode", async () => {
    const props = baseProps({ workspaceId: "ws-1", path: "docs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = (props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    expect(actions.map((a: { label: string }) => a.label)).toEqual(["Copy path", "Open in editor"]);
  });

  it("Open in editor copies the path", async () => {
    const props = baseProps({ workspaceId: "ws-1", path: "docs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = (props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    const openInEditor = actions.find((a: { id: string }) => a.id === "open-in-editor");
    openInEditor.onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("docs/x.md");
  });
});
```

Note: `renderWithQuery` and `baseProps` come from Task 6's setup already
in this file. `local-file mode` in this test suite hits the same
`fetch`-based `apiGet` path as `useHostedDoc` since `legacy-fetch.ts`
itself wraps `fetch` — the global stub in `beforeEach` covers both.

- [ ] **Step 2: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: FAIL — `setToolbar` is never called yet.

- [ ] **Step 3: Implement the toolbar**

Edit `dashboard/src/panes/spec-doc-reader/index.tsx` — add the
`setToolbar` effect, placed after `doc` is computed and before the
loading/error early returns (so the toolbar clears itself while loading,
matching interface spec §5: "Passing `[]` clears the toolbar"):

```tsx
import { useCallback } from "react";
// ...existing imports...

export default function SpecDocReaderPane({ args, setArgs, setToolbar }: PaneViewProps<SpecDocReaderArgs>) {
  // ...existing hooks through `doc`...

  const copyLabel = isLocalFile ? "Copy path" : "Copy URL";
  const copyValue = isLocalFile ? args.path! : args.url!;

  const copyToClipboard = useCallback((value: string, toastText?: string) => {
    void navigator.clipboard.writeText(value);
    if (toastText) setToast(toastText);
  }, []);

  useEffect(() => {
    if (!doc) {
      setToolbar([]);
      return;
    }
    const actions = [
      {
        id: "copy-path-or-url",
        label: copyLabel,
        onClick: () => copyToClipboard(copyValue),
      },
      ...(isLocalFile
        ? [
            {
              id: "open-in-editor",
              label: "Open in editor",
              onClick: () => copyToClipboard(copyValue, "Path copied — open in your editor."),
            },
          ]
        : []),
      ...(fullPageRoute
        ? [
            {
              id: "open-full-page",
              label: "Open full-page view",
              onClick: () => navigate(fullPageRoute),
            },
          ]
        : []),
    ];
    setToolbar(actions);
    return () => setToolbar([]);
  }, [doc, isLocalFile, copyLabel, copyValue, fullPageRoute, copyToClipboard, setToolbar]);
```

`fullPageRoute` and `navigate` are added in this same edit — full-page
detection per spec §6.1 (playbook-spec path match or `playbook_id`
frontmatter field):

```tsx
  const fullPageRoute = useMemo(() => {
    const pathOrUrl = args.path ?? args.url ?? "";
    if (/vault\/.*\/playbooks\/[^/]+\.md$/.test(pathOrUrl)) {
      const playbookId = pathOrUrl.split("/").pop()!.replace(/\.md$/, "");
      return `/settings/playbooks/${playbookId}`;
    }
    const fmPlaybookId = doc?.frontmatter?.playbook_id;
    if (typeof fmPlaybookId === "string" && fmPlaybookId) {
      return `/settings/playbooks/${fmPlaybookId}`;
    }
    return null;
  }, [args.path, args.url, doc?.frontmatter]);
```

And a local toast (matching the existing `Config.tsx` pattern — no shared
toast provider exists in this repo, see Task 7 research):

```tsx
  const [toast, setToast] = useState<string | null>(null);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);
```

...rendered at the top of the returned JSX tree, above the title block:

```tsx
      {toast && (
        <div className="border-b border-gray-800 bg-gray-900 px-4 py-1.5 text-xs text-gray-300">{toast}</div>
      )}
```

Add the two missing imports: `useNavigate` from `react-router-dom` (used
as `const navigate = useNavigate();` near the top of the component body),
and `useCallback` from `react` (already listed above).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx
git commit -m "feat(dashboard): spec-doc-reader — toolbar (copy path/url, open in editor, open full-page)"
```

---

### Task 8: Shortcuts

**Files:**
- Modify: `dashboard/src/panes/spec-doc-reader/index.tsx`
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `setShortcuts` from `PaneViewProps` (interface spec §5.2).
- Produces: nothing new consumed by later tasks.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`:

```tsx
describe("SpecDocReaderPane — shortcuts + TOC interaction", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("# Doc\n\n## Goal\n\nbody\n\n## Non-goals\n\nmore\n", { status: 200 })),
    );
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("registers scroll, page, and TOC shortcuts", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const bindings = (props.setShortcuts as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    const keys = bindings.map((b: { key: string }) => b.key);
    expect(keys).toEqual(expect.arrayContaining(["ArrowUp", "ArrowDown", "j", "k", "PageUp", "PageDown", "t"]));
  });

  it("clicking a TOC entry scrolls into view without touching location.hash", async () => {
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    const before = window.location.hash;
    const entry = await screen.findByRole("button", { name: "Goal" });
    entry.click();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(window.location.hash).toBe(before);
  });

  it("t focuses the TOC region", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const bindings = (props.setShortcuts as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    const tBinding = bindings.find((b: { key: string }) => b.key === "t");
    tBinding.onFire();
    const nav = screen.getByRole("navigation", { name: "Table of contents" });
    expect(nav.contains(document.activeElement)).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: FAIL — `setShortcuts` never called; TOC entries aren't
focusable buttons the `t` binding can reach yet.

- [ ] **Step 3: Implement shortcuts**

Edit `dashboard/src/panes/spec-doc-reader/index.tsx` — add a
`setShortcuts` effect alongside the toolbar effect from Task 7:

```tsx
  useEffect(() => {
    if (!doc) {
      setShortcuts([]);
      return;
    }
    const scrollByLine = (dir: 1 | -1) => {
      bodyRef.current?.scrollBy({ top: dir * 24 });
    };
    const scrollByPage = (dir: 1 | -1) => {
      const el = bodyRef.current;
      if (!el) return;
      el.scrollBy({ top: dir * el.clientHeight });
    };
    const focusToc = () => {
      const targetId = activeId ?? toc[0]?.id;
      const el = targetId ? tocRef.current?.querySelector<HTMLButtonElement>(`[data-toc-id="${targetId}"]`) : null;
      el?.focus();
    };

    const bindings = [
      { key: "ArrowUp", label: "Scroll up", onFire: () => scrollByLine(-1) },
      { key: "ArrowDown", label: "Scroll down", onFire: () => scrollByLine(1) },
      { key: "k", label: "Scroll up", onFire: () => scrollByLine(-1) },
      { key: "j", label: "Scroll down", onFire: () => scrollByLine(1) },
      { key: "PageUp", label: "Page up", onFire: () => scrollByPage(-1) },
      { key: "PageDown", label: "Page down", onFire: () => scrollByPage(1) },
      { key: "t", label: "Focus table of contents", onFire: focusToc },
    ];
    setShortcuts(bindings);
    return () => setShortcuts([]);
  }, [doc, toc, activeId, setShortcuts]);
```

And make TOC entries keyboard-activatable with `Enter` returning focus to
the body (spec §6.2), by adding an `onKeyDown` to each `TocList` button:

```tsx
            <button
              type="button"
              data-toc-id={entry.id}
              onClick={() => onSelect(entry.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  onSelect(entry.id);
                  (e.currentTarget.closest("[data-body-ref]") as HTMLElement | null)?.focus();
                }
              }}
              className={/* unchanged */}
            >
```

(`onSelect` already calls `scrollIntoView` — Task 4's `scrollToHeading`
— so `Enter` and click share the same path, matching spec §6.3's "TOC
entries are `<button>`s, not `<a href="#...">`".)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx
git commit -m "feat(dashboard): spec-doc-reader — scroll/TOC-focus/anchor-nav shortcuts"
```

---

### Task 9: Loading + error + not-markdown fallback — remaining edge cases

**Files:**
- Modify: `dashboard/src/panes/spec-doc-reader/index.tsx` (already has
  loading/404/403/generic-error/not-markdown/empty states from Task 6 —
  this task adds tests plus the one remaining gap: distinguishing 404 vs
  generic error from `apiGet`'s thrown `Error`, since `apiGet` doesn't
  currently attach a `status` field to the error it throws)
- Modify: `dashboard/src/api/legacy-fetch.ts` (attach `status` to thrown
  errors so callers can branch on it — currently the message embeds the
  status as text only)
- Modify: `dashboard/src/api/__tests__/legacy-fetch.test.ts` (new)
- Modify: `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `apiGet` (modified in this task).
- Produces: `ApiError` class (new, exported from `legacy-fetch.ts`) with a
  `status: number` field — any other future pane/component that calls
  `apiGet` can now branch on `error.status` instead of parsing the
  message string. This is a small, backward-compatible addition (the
  `.message` string is unchanged) so it doesn't require touching existing
  callers.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/src/api/__tests__/legacy-fetch.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { apiGet, ApiError } from "../legacy-fetch";

describe("apiGet", () => {
  it("throws ApiError with a status field on non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 404 })));
    await expect(apiGet("/api/x")).rejects.toMatchObject(
      new ApiError(404, "API 404: nope"),
    );
  });

  it("resolves with parsed JSON on 2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 })),
    );
    await expect(apiGet("/api/x")).resolves.toEqual({ ok: true });
  });
});
```

Append to `dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx`:

```tsx
describe("SpecDocReaderPane — loading + error + edge cases", () => {
  it("shows a loading skeleton while the query is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // never resolves
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    expect(screen.getByTestId("spec-doc-reader-skeleton")).toBeInTheDocument();
  });

  it("renders a not-found state with the path in the message on 404", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 404 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/missing.md" })} />);
    expect(await screen.findByText(/Spec not found/)).toBeInTheDocument();
    expect(await screen.findByText("docs/missing.md")).toBeInTheDocument();
  });

  it("renders a 403 access-denied state with no retry button", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("forbidden", { status: 403 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/x.md" })} />);
    expect(await screen.findByText(/don't have access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("renders a generic error with a working Retry on 5xx", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("boom", { status: 500 }))
      .mockResolvedValueOnce(new Response("# Doc\n\nbody\n", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    const retry = await screen.findByRole("button", { name: "Retry" });
    retry.click();
    expect(await screen.findByText("Doc")).toBeInTheDocument();
  });

  it("renders a <pre> fallback for non-markdown content with no TOC/meta card", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("plain text, no headings", { status: 200 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/notes.txt" })} />);
    const pre = await screen.findByText("plain text, no headings");
    expect(pre.tagName).toBe("PRE");
    expect(screen.queryByRole("navigation", { name: "Table of contents" })).toBeNull();
  });

  it("renders the empty state when the post-frontmatter body is empty", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("---\nstatus: design\n---\n", { status: 200 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    expect(await screen.findByText("This document is empty.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx dashboard/src/api/__tests__/legacy-fetch.test.ts`

Expected: FAIL — `ApiError` doesn't exist yet; the loading skeleton has
no `data-testid`; 404 detection currently can't distinguish status from
`apiGet`'s plain `Error`.

- [ ] **Step 3: Add `ApiError` to `legacy-fetch.ts`**

Edit `dashboard/src/api/legacy-fetch.ts`:

```ts
const BASE_URL = import.meta.env.VITE_API_URL || "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function apiGet<T = unknown>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) {
    throw new ApiError(res.status, `API ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

// ...legacyFetch unchanged below...
```

- [ ] **Step 4: Add a `data-testid` to the loading skeleton and fix error
      branching in `index.tsx`**

Edit the loading branch:

```tsx
  if (fileQuery.isLoading) {
    return (
      <div className="p-4" data-testid="spec-doc-reader-skeleton">
```

Edit the error branch to read `error.status` off `ApiError` instead of
the old duck-typed cast:

```tsx
  if (fileQuery.isError) {
    const err = fileQuery.error as { status?: number };
    const pathOrUrl = args.path ?? args.url ?? "";
    if (err.status === 404) {
      return <div className="p-4 text-sm text-gray-400">Spec not found at <code>{pathOrUrl}</code></div>;
    }
    if (err.status === 403) {
      return <div className="p-4 text-sm text-red-400">You don&apos;t have access to this workspace.</div>;
    }
    return (
      <div className="p-4 text-sm text-red-400">
        Couldn&apos;t load this document.
        <button type="button" onClick={() => fileQuery.refetch()} className="ml-2 rounded border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-800">
          Retry
        </button>
      </div>
    );
  }
```

`useHostedDoc`'s `HostedDocFetchError` (Task 6) also needs a `status`
field of the same name for this to work uniformly across both modes — it
already has one (`this.status = status`), so no change needed there.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npm run test --prefix dashboard -- spec-doc-reader/__tests__/index.test.tsx dashboard/src/api/__tests__/legacy-fetch.test.ts`

Expected: PASS.

- [ ] **Step 6: Full pane test-suite regression check**

Run: `npm run test --prefix dashboard -- spec-doc-reader`

Expected: every test across `manifest.test.ts`, `docProcessing.test.ts`,
and `index.test.tsx` passes.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/panes/spec-doc-reader/index.tsx \
        dashboard/src/api/legacy-fetch.ts \
        dashboard/src/api/__tests__/legacy-fetch.test.ts \
        dashboard/src/panes/spec-doc-reader/__tests__/index.test.tsx
git commit -m "feat(dashboard): spec-doc-reader — loading/error/not-markdown/empty edge cases"
```

---

### Task 10: Manifest tests + component tests + registry parity — audit pass

This task adds no new production code. It is the gate that confirms every
requirement in §10 of the spec has a passing test, and that the frontend
and backend pane registries agree.

**Files:** none created; verification only.

- [ ] **Step 1: Run the full dashboard test suite**

Run: `npm run test --prefix dashboard`

Expected: 100% pass, including everything from Tasks 1–9.

- [ ] **Step 2: Cross-check spec §10 against the test files**

Read `docs/superpowers/specs/2026-08-22-pane-spec-doc-reader-design.md`
§10 line by line against
`dashboard/src/panes/spec-doc-reader/__tests__/manifest.test.ts`,
`docProcessing.test.ts`, and `index.test.tsx`. Confirm each of the
following has a corresponding passing test (all were added in Tasks
1–9 — this step is the audit, not new authorship):

- Manifest: id/dir match, args_schema accept/reject matrix, no
  `open_shortcut` — Task 1.
- Local-file mode: loading skeleton, title+meta+TOC+body render, `close`
  wiring, copy path, open-in-editor present+copies+toast, open-in-editor
  absent in url mode, 404 with path in message — Tasks 6, 7, 9.
- Url mode: mocked-fetch render, copy URL, network error + working Retry
  — Tasks 6, 7, 9.
- TOC: only ##/### extracted, ids match rendered heading ids (slug
  parity), click doesn't touch `location.hash`, `t` focuses TOC, `Enter`
  on a focused entry scrolls — Tasks 4, 8.
- Frontmatter: fenced YAML → meta card, absent → no card, bold-label
  fallback → same card — Task 5.
- Edge cases: non-markdown `<pre>` with no TOC/meta, `truncated: true`
  banner, empty post-frontmatter body — Tasks 6, 9.

If any bullet has no covering test, stop and add it before proceeding
(this would be a plan-authoring gap, not expected in normal execution
since each bullet is traced to the task that added it above).

One item from spec §10 is **not** separately tested and that is
intentional, not a gap: "`close()` fires from the shell close button" —
the shell close button is `<ShellPane>`'s header chrome (owned by the
shell, not this view — interface spec §5.1: "Header is provided by the
shell"), and `close` is a prop this view never renders its own button
for. There is nothing pane-specific to test here beyond confirming
`close` is accepted as a prop, which the TypeScript compiler already
enforces via `PaneViewProps<SpecDocReaderArgs>`.

- [ ] **Step 3: Run the registry parity test**

Run: `pytest tests/test_pane_registry_parity.py -v`

Expected: PASS (already run once in Task 2 — re-run here as part of the
consolidated gate since later tasks touched `index.tsx`, not
`manifest.ts`, so this should be unaffected, but confirming costs
nothing).

- [ ] **Step 4: Run the shared frontend registry test, if present**

Run: `npm run test --prefix dashboard -- panes/__tests__/registry.test.ts`

This file is shared infrastructure owned by the pane-plugin-interface
plan (interface spec §9.2) — it should already exist and already pass
per Prerequisites. If it exists, confirm `spec-doc-reader` shows up as a
resolvable, uniquely-id'd entry with no `open_shortcut` collision. If it
does not exist yet, that's a Prerequisites violation — stop and escalate
rather than authoring shared registry-test infrastructure inside this
pane's plan.

- [ ] **Step 5: Typecheck + lint**

Run: `npm run typecheck --prefix dashboard && npm run lint --prefix dashboard`

Expected: clean. Pay particular attention to `PaneViewProps<SpecDocReaderArgs>`
usage in `index.tsx` — a mismatched generic here (e.g. forgetting
`setArgs`'s type) is exactly the kind of bug the interface spec's
component-contract typing is meant to catch (§5, "Use `PaneViewProps<TArgs>`
typing to catch mismatches").

- [ ] **Step 6: Commit (only if Step 2 required additions)**

If Step 2 found no gaps, there is nothing to commit for this task — skip
straight to Task 11. If it did, commit the additions with:

```bash
git add dashboard/src/panes/spec-doc-reader/__tests__/
git commit -m "test(dashboard): spec-doc-reader — close remaining §10 coverage gaps"
```

---

### Task 11: Manual verification checklist

No code changes. Run the dashboard against a live daemon and walk through
this checklist by hand — automated tests cover logic, not the actual
feel of scrolling, resizing, and agent-push arrival.

**Setup:**

```bash
./run.sh start              # from repo root — starts the daemon
npm run dev --prefix dashboard   # vite dev server on :5173, proxies /api
```

- [ ] **Step 1: Local-file mode via palette**

Open the palette (`⌘K`), invoke "Read spec" under the Docs section. Per
spec §3, with no prior focus context this is a documented no-op today
(console warning) — confirm the console warning appears and nothing
crashes, rather than expecting the pane to open. This is expected
behavior, not a bug, until args-prompting ships (interface spec §11).

- [ ] **Step 2: Local-file mode via direct `open()` call**

From the browser devtools console (or a temporary debug button, removed
before commit), call:

```js
window.__shellPaneDebugOpen?.("spec-doc-reader", {
  workspaceId: "<a real attached workspace id>",
  path: "docs/superpowers/specs/2026-08-22-pane-spec-doc-reader-design.md",
});
```

(If no such debug hook exists, trigger `open()` via whatever click-through
surface is available at the time — e.g. a file-browser pane click-through
if that view has landed.) Confirm:
- Title resolves to "Pane View: `spec-doc-reader` — Design" (from the h1,
  since this repo's specs don't use fenced frontmatter).
- Meta card shows Status "design" and the Depends-on/Ship-priority rows
  from the bold-label preamble.
- TOC lists all `##` headings (Goal, Non-goals, Manifest, Args +
  validation, Component, ... ) with `###` sub-entries indented under
  their parent (e.g. "5.1 Layout" under "Component").
- Clicking a TOC entry smooth-scrolls the body; the URL bar's hash does
  not change.
- Resizing the pane narrower than ~420px collapses the TOC into a
  `<details>` disclosure above the body; widening restores the sidebar.

- [ ] **Step 3: Url mode**

Open with `{ url: "/api/specs/2026-08-22-pane-plugin-interface-design.md" }`
(or whatever route actually serves specs at verification time — if none
exists yet, this step blocks on that route shipping; note the gap rather
than skipping the check silently). Confirm content renders identically
to local-file mode modulo the toolbar (no "Open in editor" button, "Copy
URL" instead of "Copy path").

- [ ] **Step 4: Keyboard**

With the pane focused: `↑↓`/`j`/`k` scroll a line at a time; `PgUp`/`PgDn`
scroll a page; `t` moves focus into the TOC (visually confirm a focus
ring lands on the active or first entry); `Enter` on a focused TOC entry
scrolls the body and returns focus there (confirm by immediately pressing
`j` and seeing the body scroll, not the TOC list move).

- [ ] **Step 5: Toolbar**

Click "Copy path"/"Copy URL" and paste into a scratch field — confirm the
right value landed. In local-file mode, click "Open in editor" and
confirm the toast "Path copied — open in your editor." appears and
auto-dismisses after ~3s.

- [ ] **Step 6: Full-page view button**

Open a doc whose path matches `vault/**/playbooks/*.md` (or set
`playbook_id` in a test doc's frontmatter) — confirm "Open full-page
view" appears and navigates to `/settings/playbooks/<id>`. Open a normal
`docs/superpowers/specs/*.md` doc — confirm the button is absent (per
spec §6.1/§12, no general spec route exists yet).

- [ ] **Step 7: Edge cases**

- Open a path that 404s (typo a filename) — confirm "Spec not found at
  `<path>`" with Copy path still working.
- Kill the daemon mid-load and open a fresh doc — confirm the generic
  error state with a working Retry (restart the daemon, click Retry,
  confirm it recovers).
- Open a non-markdown file (e.g. a `.json` config under an attached
  workspace) — confirm it renders as plain `<pre>` with no TOC/meta card.
- Open an empty `.md` file — confirm "This document is empty."

- [ ] **Step 8: Agent-push arrival**

From a shell with `aq` on PATH and the daemon running:

```bash
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Here's the shell spec →" \
    --pane-open '{"view": "spec-doc-reader", "args": {"workspaceId": "ws-vault", "path": "docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md"}}'
```

Confirm the pane auto-opens (client `to_kind === "user"` check passes)
and an inline "opened →" chip appears in the chat transcript. Closing the
pane and clicking the chip reopens it with the same args.

- [ ] **Step 9: Report findings**

Note any deviations from this checklist (broken step, missing upstream
route, unexpected visual bug) back to whoever is tracking this plan's
execution — do not silently patch around a broken assumption from an
earlier task without recording why.
