# Pane View — `contextual-settings` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `contextual-settings` pane view — a polymorphic settings
editor (`project` / `profile` / `project-profile` / `playbook` /
`intelligence-class`) that renders inline in the dashboard's right-side pane,
reusing every existing form/save hook with zero new mutation logic.

**Architecture:** A thin `index.tsx` subject switch dispatches to five
per-subject components under `dashboard/src/panes/contextual-settings/subjects/`,
each a pane-sized re-render of an existing drawer/page's body wired to its
existing React Query hooks. Three small shared extractions (`FormSection.tsx`,
`Config.tsx`'s form helpers, `useIntelligenceClasses`) remove duplication
that would otherwise be copy-pasted a third time. Because
`dashboard/src/panes/` does not exist yet in this repo (see Global
Constraints), this plan also lays the minimal pane-plugin scaffold
(`types.ts`, `registry.ts`) needed to host this one view — it does not build
the full shell (`<ShellPane>`, palette, `useShortcuts`) from the shell spec,
which remains out of scope per the pane-plugin-interface spec's own
non-goals ("a view never needs to touch shell code").

**Tech Stack:** React 19, TypeScript 5.7 (strict, `noUncheckedIndexedAccess`),
Vite 6, TanStack Query 5, Tailwind v4, `@heroicons/react` 2, `zod` (new dep),
`react-router-dom` 7. Backend: Python 3.12, pytest.

**Spec:**
- `docs/superpowers/specs/2026-08-22-pane-contextual-settings-design.md` (primary)
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (contract)
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (context only)

## Global Constraints

- Dashboard is React 19 + TypeScript strict (`noUncheckedIndexedAccess`,
  `noUnusedLocals`, `noUnusedParameters`) — every new file must typecheck
  under `npm run typecheck` (`tsc -b --noEmit`).
- Icons: `@heroicons/react/24/outline` only — never `lucide-react` or any
  other icon package, even though the plugin-interface spec's
  `PaneToolbarAction.icon` example types it as `LucideIcon` (a spec bug this
  plan corrects — see Task 2).
- Daemon I/O: never call `fetch` directly for endpoints that exist in the
  generated SDK — use `../api/client` or a hook in `../api/hooks`. The one
  documented exception is `legacy-fetch.ts`, reserved for routes with no
  generated SDK entry (`list-intelligence-classes` is such a route today —
  see Task 6).
- React Query key convention: `[entity, ...filters]`; mutations invalidate
  relevant list + detail queries on success.
- Project field names match the daemon exactly: `repo_url`,
  `repo_default_branch`, `discord_channel_id` — never renamed.
- `repo_url` has no edit path in `EditProjectRequest` — render read-only
  everywhere, per spec §2/§13.
- Every pane view directory follows
  `dashboard/src/panes/<view-id>/{manifest.ts,args.ts,index.tsx,__tests__/}`
  per the plugin-interface spec §3.
- **Deviation (recorded, not to be "fixed" mid-plan):** `dashboard/src/panes/`,
  `src/panes/registry.py`, and any frontend test runner do not exist in this
  repo yet (Phase B of the shell spec has not landed). This plan creates the
  minimal slice of each needed to ship this one view, and nothing more —
  `<ShellPane>`, `useShellPane`, the command palette, and `useShortcuts` are
  out of scope and are not built here. `setToolbar`/`setShortcuts`/`close`/
  `setArgs` are consumed purely as **props** of type `PaneViewProps`, which
  is how the plugin-interface spec says a view must consume them regardless
  of what provides them — so this view compiles and is fully testable with
  no shell present.

---

## Task 1: Frontend test runner (Vitest + RTL)

No test runner exists anywhere in `dashboard/` today (no `vitest`, no
`@testing-library/*`, no `test` script, zero `*.test.ts*` files). Every
later task in this plan needs one. This task adds it and proves it works
with a throwaway smoke test (removed at the end of the task).

**Files:**
- Modify: `dashboard/package.json`
- Modify: `dashboard/vite.config.ts`
- Create: `dashboard/src/test/setup.ts`
- Create (temporary, deleted in Step 6): `dashboard/src/test/__smoke__/smoke.test.ts`

**Interfaces:**
- Produces: `npm run test` (from `dashboard/`) runs Vitest once; `npm run
  test:watch` runs it in watch mode. `@testing-library/jest-dom` matchers
  (`toBeInTheDocument`, etc.) are globally available in every `*.test.ts(x)`
  file via `src/test/setup.ts`.

- [ ] **Step 1: Add dependencies**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
npm install --save zod
npm install --save-dev vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
```

- [ ] **Step 2: Write the smoke test**

```ts
// dashboard/src/test/__smoke__/smoke.test.ts
import { describe, expect, it } from "vitest";

describe("vitest smoke", () => {
  it("runs and jest-dom matchers are registered", () => {
    document.body.innerHTML = "<p>hi</p>";
    expect(document.body).toHaveTextContent("hi");
  });
});
```

- [ ] **Step 3: Wire config**

```ts
// dashboard/vite.config.ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Read via globalThis so this config typechecks without @types/node.
const target =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_API_TARGET ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/health": target,
      "/ready": target,
      "/ws": { target, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
```

(Only the `import` source and the trailing `test` block are new —
`plugins`/`server` are unchanged from today's file.)

```ts
// dashboard/src/test/setup.ts
import "@testing-library/jest-dom/vitest";
```

```json
// dashboard/package.json — add under "scripts"
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 4: Run it, verify pass**

Run: `cd dashboard && npm run test`
Expected: `1 passed` (the smoke test).

- [ ] **Step 5: Run typecheck to confirm nothing broke**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Delete the smoke test**

```bash
rm -rf /home/jkern/dev/agent-queue2/dashboard/src/test/__smoke__
```

- [ ] **Step 7: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add package.json package-lock.json vite.config.ts src/test/setup.ts
git commit -m "test(dashboard): add Vitest + RTL test runner"
```

---

## Task 2: Pane plugin scaffold — `dashboard/src/panes/types.ts`

The plugin-interface spec's contract (`PaneManifest`, `PaneViewProps`,
`PaneToolbarAction`, `ShortcutBinding`) has no home in this repo yet. Create
it, correcting the spec's `LucideIcon` typo to the heroicons-only type this
dashboard actually uses (per Global Constraints).

**Files:**
- Create: `dashboard/src/panes/types.ts`
- Test: `dashboard/src/panes/__tests__/types.test.ts`

**Interfaces:**
- Produces: `PaneManifest<TArgs>`, `PaneViewProps<TArgs>`,
  `PaneToolbarAction`, `ShortcutBinding`, `HeroIcon` — every later pane file
  imports these from `dashboard/src/panes/types.ts`.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/__tests__/types.test.ts
import { describe, expect, it } from "vitest";
import type { PaneManifest, PaneViewProps } from "../types";
import { z } from "zod";
import { Cog6ToothIcon } from "@heroicons/react/24/outline";

describe("pane types", () => {
  it("PaneManifest accepts a heroicon component and a zod args schema", () => {
    const schema = z.object({ subjectId: z.string() });
    const manifest: PaneManifest<z.infer<typeof schema>> = {
      id: "example",
      name: "Example",
      description: "An example view.",
      icon: Cog6ToothIcon,
      args_schema: schema,
      route_scope: "cross-route",
      agent_pushable: true,
      palette_label: "Open example…",
      palette_section: "Panes",
    };
    expect(manifest.id).toBe("example");
  });

  it("PaneViewProps shape accepts the five required callbacks", () => {
    const props: PaneViewProps<{ x: number }> = {
      args: { x: 1 },
      close: () => {},
      setArgs: () => {},
      setToolbar: () => {},
      setShortcuts: () => {},
    };
    expect(typeof props.close).toBe("function");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/__tests__/types.test.ts`
Expected: FAIL — `Cannot find module '../types'`.

- [ ] **Step 3: Write the implementation**

```ts
// dashboard/src/panes/types.ts
import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

/**
 * This dashboard is standardized on heroicons
 * (`@heroicons/react/24/outline`). Every icon prop in the pane contract
 * uses this type — never `lucide-react`'s `LucideIcon`, despite what an
 * earlier draft of the plugin-interface spec's `PaneToolbarAction.icon`
 * example showed.
 */
export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>;

export interface PaneManifest<TArgs = unknown> {
  /** Stable id — matches the directory name; used everywhere. */
  id: string;
  /** Human name shown in the pane header + palette. */
  name: string;
  /** Short description used in palette + cheat sheet. */
  description: string;
  /** Icon shown in header + palette. */
  icon: HeroIcon;
  /** zod schema for the args object. Runtime-validated on every open call. */
  args_schema?: z.ZodType<TArgs>;
  /**
   * Optional keyboard shortcut that OPENS this view. Omit the field
   * entirely when a view has no open shortcut — do NOT set it to `null`
   * (the field's type is `string | undefined`, not `string | null`).
   */
  open_shortcut?: string;
  /**
   * "cross-route" (default): pane content persists across route
   * navigation. "route-scoped": pane closes automatically on route change.
   */
  route_scope?: "cross-route" | "route-scoped";
  /** Whether the agent may push this view via the pane_open message frame. */
  agent_pushable?: boolean;
  /** Palette action label. `null` means: don't register a palette action. */
  palette_label?: string | null;
  /** Palette section this view's action belongs to (default: "Panes"). */
  palette_section?: string;
}

export interface PaneToolbarAction {
  id: string;
  label: string;
  icon?: HeroIcon;
  onClick: () => void;
  disabled?: boolean;
}

export interface ShortcutBinding {
  /** Normalized form, e.g. "$mod-s". */
  key: string;
  /** Shown in the cheat sheet. */
  label: string;
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  /** The args object passed at open time, already zod-validated. */
  args: TArgs;
  /** Close the pane. */
  close: () => void;
  /** Update the args for THIS OPEN pane without closing + re-opening. */
  setArgs: (next: TArgs) => void;
  /** Register toolbar action buttons in the pane header. Passing `[]` clears. */
  setToolbar: (actions: PaneToolbarAction[]) => void;
  /** Register per-entity shortcuts scoped to this pane. */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/__tests__/types.test.ts`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/types.ts src/panes/__tests__/types.test.ts
git commit -m "feat(dashboard): pane plugin contract types (PaneManifest, PaneViewProps)"
```

---

## Task 3: Extract `Section`/`Field` into `FormSection.tsx`

`SystemProfileEditDrawer.tsx` (lines 224–251) and `ProfileEditDrawer.tsx`
(lines 230–257) each define byte-identical `Section` and `Field`
components. Extract once; both drawers import from the new file
(regression-safe — same markup, same classNames).

**Files:**
- Create: `dashboard/src/components/profile/FormSection.tsx`
- Modify: `dashboard/src/components/profile/SystemProfileEditDrawer.tsx`
- Modify: `dashboard/src/components/profile/ProfileEditDrawer.tsx`
- Test: `dashboard/src/components/profile/__tests__/FormSection.test.tsx`

**Interfaces:**
- Produces: `Section({ title, hint?, children })`, `Field({ label, children })`
  — named exports from `dashboard/src/components/profile/FormSection.tsx`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/components/profile/__tests__/FormSection.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Section, Field } from "../FormSection";

describe("FormSection", () => {
  it("Section renders title, optional hint, and children", () => {
    render(
      <Section title="Basics" hint="some hint">
        <p>child content</p>
      </Section>,
    );
    expect(screen.getByText("Basics")).toBeInTheDocument();
    expect(screen.getByText("some hint")).toBeInTheDocument();
    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it("Field renders a label and children", () => {
    render(
      <Field label="Name">
        <input aria-label="Name" />
      </Field>,
    );
    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/components/profile/__tests__/FormSection.test.tsx`
Expected: FAIL — `Cannot find module '../FormSection'`.

- [ ] **Step 3: Write `FormSection.tsx`** (verbatim body from both drawers)

```tsx
// dashboard/src/components/profile/FormSection.tsx
import type { ReactNode } from "react";

export function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500">{title}</h3>
        {hint && <p className="mt-0.5 text-xs text-gray-600">{hint}</p>}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium uppercase text-gray-500">{label}</label>
      {children}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/components/profile/__tests__/FormSection.test.tsx`
Expected: `2 passed`.

- [ ] **Step 5: Update both drawers to import instead of define**

In `dashboard/src/components/profile/SystemProfileEditDrawer.tsx`:

Delete lines 224–251 (the local `Section`/`Field` definitions) and add the
import:

```tsx
import { Section, Field } from "./FormSection";
```

(placed alongside the existing `IntelligenceClassPicker`/`McpServerSelector`/
`ToolPicker` imports at the top of the file).

In `dashboard/src/components/profile/ProfileEditDrawer.tsx`: identical
change — delete lines 230–257, add the same import line.

- [ ] **Step 6: Typecheck + run full test suite to confirm no regressions**

Run: `cd dashboard && npm run typecheck && npm run test`
Expected: both exit 0 / all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/components/profile/FormSection.tsx \
  src/components/profile/SystemProfileEditDrawer.tsx \
  src/components/profile/ProfileEditDrawer.tsx \
  src/components/profile/__tests__/FormSection.test.tsx
git commit -m "refactor(dashboard): extract shared Section/Field into FormSection.tsx"
```

---

## Task 4: Extract `profileToForm` mapper

Both drawers also duplicate `profileToForm` (and its `FormState` type)
byte-for-byte. Promote to a shared module next to `FormSection.tsx`.

**Files:**
- Create: `dashboard/src/components/profile/profileForm.ts`
- Modify: `dashboard/src/components/profile/SystemProfileEditDrawer.tsx`
- Modify: `dashboard/src/components/profile/ProfileEditDrawer.tsx`
- Test: `dashboard/src/components/profile/__tests__/profileForm.test.ts`

**Interfaces:**
- Consumes: `ProfileDetail` from `dashboard/src/api/hooks.ts` (existing).
- Produces: `interface ProfileFormState { name, description, default_class,
  permission_mode, system_prompt_suffix, allowed_tools: string[],
  mcp_servers: string[] }` and `function profileToForm(p: ProfileDetail |
  null | undefined): ProfileFormState` — named exports from
  `dashboard/src/components/profile/profileForm.ts`. Both editable profile
  subjects in Task 12/13 import `ProfileFormState` (aliased from
  `FormState` in the source drawers, renamed here to avoid colliding with
  `Config.tsx`'s own `FormState` once both are imported side by side).

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/components/profile/__tests__/profileForm.test.ts
import { describe, expect, it } from "vitest";
import { profileToForm } from "../profileForm";
import type { ProfileDetail } from "../../../api/hooks";

describe("profileToForm", () => {
  it("maps a full profile", () => {
    const profile = {
      name: "Reviewer",
      description: "Reviews PRs",
      default_class: "standard-medium",
      permission_mode: "acceptEdits",
      system_prompt_suffix: "Be terse.",
      allowed_tools: ["Read", "Edit"],
      mcp_servers: ["aq-files"],
    } as unknown as ProfileDetail;
    expect(profileToForm(profile)).toEqual({
      name: "Reviewer",
      description: "Reviews PRs",
      default_class: "standard-medium",
      permission_mode: "acceptEdits",
      system_prompt_suffix: "Be terse.",
      allowed_tools: ["Read", "Edit"],
      mcp_servers: ["aq-files"],
    });
  });

  it("maps null/undefined to empty defaults", () => {
    expect(profileToForm(null)).toEqual({
      name: "",
      description: "",
      default_class: "",
      permission_mode: "",
      system_prompt_suffix: "",
      allowed_tools: [],
      mcp_servers: [],
    });
  });

  it("maps the sentinel '(default)' permission_mode to empty string", () => {
    const profile = { permission_mode: "(default)" } as unknown as ProfileDetail;
    expect(profileToForm(profile).permission_mode).toBe("");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/components/profile/__tests__/profileForm.test.ts`
Expected: FAIL — `Cannot find module '../profileForm'`.

- [ ] **Step 3: Write `profileForm.ts`** (verbatim logic from both drawers,
  type renamed `FormState` → `ProfileFormState` to avoid collision with
  `Config.tsx`'s own `FormState`, per Task 5)

```ts
// dashboard/src/components/profile/profileForm.ts
import type { ProfileDetail } from "../../api/hooks";

export interface ProfileFormState {
  name: string;
  description: string;
  default_class: string;
  permission_mode: string;
  system_prompt_suffix: string;
  allowed_tools: string[];
  mcp_servers: string[];
}

export function profileToForm(p: ProfileDetail | null | undefined): ProfileFormState {
  const dc = (p as { default_class?: string } | null | undefined)?.default_class;
  const rawPerm = p?.permission_mode ?? "";
  return {
    name: p?.name ?? "",
    description: p?.description ?? "",
    default_class: dc ?? "",
    permission_mode: rawPerm === "(default)" ? "" : rawPerm,
    system_prompt_suffix: p?.system_prompt_suffix ?? "",
    allowed_tools: [...(p?.allowed_tools ?? [])],
    mcp_servers: [...(p?.mcp_servers ?? [])],
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/components/profile/__tests__/profileForm.test.ts`
Expected: `3 passed`.

- [ ] **Step 5: Update both drawers**

In `SystemProfileEditDrawer.tsx`: delete the local `interface FormState`
(lines 18–26) and `function profileToForm` (lines 28–40); add:

```tsx
import { profileToForm, type ProfileFormState as FormState } from "./profileForm";
```

(aliasing back to the local name `FormState` keeps the rest of the file's
`useState<FormState>` / `set<K extends keyof FormState>` code unchanged —
no other line in the drawer needs to change).

In `ProfileEditDrawer.tsx`: identical change — delete lines 19–27 and
29–41, add the same import.

- [ ] **Step 6: Typecheck + full test suite**

Run: `cd dashboard && npm run typecheck && npm run test`
Expected: both exit 0 / all pass.

- [ ] **Step 7: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/components/profile/profileForm.ts \
  src/components/profile/SystemProfileEditDrawer.tsx \
  src/components/profile/ProfileEditDrawer.tsx \
  src/components/profile/__tests__/profileForm.test.ts
git commit -m "refactor(dashboard): extract shared profileToForm mapper"
```

---

## Task 5: Promote `Config.tsx`'s form helpers to named exports

`dashboard/src/pages/project/Config.tsx` defines `FormState`,
`parseOptionalInt`, `parseOptionalFloat`, `projectToForm`, and an inline
profile-option dedup (lines 59–61) — all needed verbatim by
`ProjectSubject.tsx` (Task 11). Promote them; also extract the dedup into a
named helper both `Config.tsx` and `ProjectSubject.tsx` call, removing a
second inline duplication before it's created.

**Files:**
- Modify: `dashboard/src/pages/project/Config.tsx`
- Test: `dashboard/src/pages/project/__tests__/Config.helpers.test.ts`

**Interfaces:**
- Produces (all named exports of `dashboard/src/pages/project/Config.tsx`):
  `interface FormState { name, repo_default_branch, default_profile_id,
  max_concurrent_agents, credit_weight, budget_limit, discord_channel_id:
  string }`; `interface ProjectData { name?, repo_default_branch?,
  default_profile_id?, max_concurrent_agents?, credit_weight?,
  budget_limit?, discord_channel_id? }`; `function parseOptionalInt(v:
  string): number | null`; `function parseOptionalFloat(v: string): number
  | null`; `function projectToForm(p: ProjectData): FormState`; `function
  profileOptionsFromRows(rows: { scoped?: {id,name} | null; global?:
  {id,name} | null }[]): { id: string; name: string }[]`.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/pages/project/__tests__/Config.helpers.test.ts
import { describe, expect, it } from "vitest";
import {
  parseOptionalInt,
  parseOptionalFloat,
  projectToForm,
  profileOptionsFromRows,
} from "../Config";

describe("Config.tsx form helpers", () => {
  it("parseOptionalInt parses, trims, and nulls on empty/invalid", () => {
    expect(parseOptionalInt(" 4 ")).toBe(4);
    expect(parseOptionalInt("")).toBeNull();
    expect(parseOptionalInt("abc")).toBeNull();
  });

  it("parseOptionalFloat parses, trims, and nulls on empty/invalid", () => {
    expect(parseOptionalFloat(" 4.5 ")).toBe(4.5);
    expect(parseOptionalFloat("")).toBeNull();
    expect(parseOptionalFloat("abc")).toBeNull();
  });

  it("projectToForm maps nulls to empty strings and numbers to strings", () => {
    expect(projectToForm({})).toEqual({
      name: "",
      repo_default_branch: "",
      default_profile_id: "",
      max_concurrent_agents: "",
      credit_weight: "",
      budget_limit: "",
      discord_channel_id: "",
    });
    expect(projectToForm({ max_concurrent_agents: 3, credit_weight: 1.5 }).max_concurrent_agents).toBe(
      "3",
    );
  });

  it("profileOptionsFromRows dedupes scoped+global by id", () => {
    const rows = [
      { scoped: { id: "coder", name: "Coder (scoped)" }, global: { id: "coder", name: "Coder" } },
      { scoped: null, global: { id: "reviewer", name: "Reviewer" } },
    ];
    const opts = profileOptionsFromRows(rows);
    expect(opts.map((o) => o.id)).toEqual(["coder", "reviewer"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/pages/project/__tests__/Config.helpers.test.ts`
Expected: FAIL — named exports don't exist yet (all four are currently
unexported / `profileOptionsFromRows` doesn't exist).

- [ ] **Step 3: Edit `Config.tsx`**

Change (lines 19–27) `interface FormState` → `export interface FormState`.

Add `export` to `interface ProjectData` (lines 366–374) and to `function
projectToForm` (line 376), `function parseOptionalInt` (line 389),
`function parseOptionalFloat` (line 396).

Add the new helper (place it directly above `projectToForm`, since both are
"derive from server data" helpers):

```ts
export function profileOptionsFromRows(
  rows: { scoped?: { id: string; name: string } | null; global?: { id: string; name: string } | null }[],
): { id: string; name: string }[] {
  return rows
    .flatMap((row) => [row.scoped, row.global].filter(Boolean) as { id: string; name: string }[])
    .filter((p, i, arr) => arr.findIndex((q) => q.id === p.id) === i);
}
```

Replace the inline computation at line 59–61 (`const profileOptions =
(profiles?.agent_types ?? []).flatMap(...).filter(...)`) with:

```tsx
const profileOptions = profileOptionsFromRows(profiles?.agent_types ?? []);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/pages/project/__tests__/Config.helpers.test.ts`
Expected: `4 passed`.

- [ ] **Step 5: Typecheck + full suite**

Run: `cd dashboard && npm run typecheck && npm run test`
Expected: both exit 0 / all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/pages/project/Config.tsx src/pages/project/__tests__/Config.helpers.test.ts
git commit -m "refactor(dashboard): promote Config.tsx form helpers to named exports"
```

---

## Task 6: Shared `useIntelligenceClasses` hook

`IntelligenceClassPicker.tsx` and `IntelligenceClassesStub.tsx` each own an
inline `legacyFetch("/api/system/list-intelligence-classes", …)` call under
the same React Query key `["intelligence-classes"]` but with different
`staleTime` (60_000 vs 30_000) and different `queryFn` return shapes (array
vs full response object). Consolidate into one hook in `hooks.ts`; both
call sites switch to it.

There is no generated-SDK type for this endpoint (no Pydantic response
model is registered for it server-side — confirmed via
`src/api/models/*.py`), so this hook continues to use `legacyFetch`,
matching the two existing call sites — this is the one documented exception
to "never call fetch directly", not a new violation, and registering a
proper backend model is explicitly out of scope for this view (spec §2).

**Files:**
- Modify: `dashboard/src/api/hooks.ts`
- Modify: `dashboard/src/components/profile/IntelligenceClassPicker.tsx`
- Modify: `dashboard/src/pages/settings/IntelligenceClassesStub.tsx`
- Test: `dashboard/src/api/__tests__/useIntelligenceClasses.test.tsx`

**Interfaces:**
- Produces (from `dashboard/src/api/hooks.ts`):
  ```ts
  export type ProviderSlice = {
    model?: string;
    thinking?: string;
    reasoning_effort?: string;
    thinking_budget?: number;
  };
  export type IntelligenceClassRow = {
    id: string;
    name: string;
    description: string;
    mapping: Record<string, ProviderSlice>;
  };
  export type IntelligenceClassesResponse = { success: boolean; classes: IntelligenceClassRow[] };
  export function useIntelligenceClasses(): UseQueryResult<IntelligenceClassesResponse>;
  ```
  Consumed directly by Task 15's `IntelligenceClassSubject.tsx`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/api/__tests__/useIntelligenceClasses.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useIntelligenceClasses } from "../hooks";
import * as legacyFetchModule from "../legacy-fetch";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useIntelligenceClasses", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches and returns the full response shape", async () => {
    const body = {
      success: true,
      classes: [
        { id: "fast-off", name: "Fast", description: "", mapping: { anthropic: { model: "haiku" } } },
      ],
    };
    vi.spyOn(legacyFetchModule, "legacyFetch").mockResolvedValue({
      ok: true,
      json: async () => body,
      text: async () => "",
    } as Response);

    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);
  });

  it("throws on a non-ok response", async () => {
    vi.spyOn(legacyFetchModule, "legacyFetch").mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({}),
      text: async () => "boom",
    } as Response);

    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/api/__tests__/useIntelligenceClasses.test.tsx`
Expected: FAIL — `useIntelligenceClasses` is not exported from `../hooks`.

- [ ] **Step 3: Add the hook to `hooks.ts`**

Add near the other read hooks (e.g. after `useProjectProfiles`, since both
are profile-adjacent), and add the three new types to the `export type {
... }` re-export block (lines 147–177):

```ts
// dashboard/src/api/hooks.ts
import { legacyFetch } from "./legacy-fetch";

export type ProviderSlice = {
  model?: string;
  thinking?: string;
  reasoning_effort?: string;
  thinking_budget?: number;
};

export type IntelligenceClassRow = {
  id: string;
  name: string;
  description: string;
  mapping: Record<string, ProviderSlice>;
};

export type IntelligenceClassesResponse = {
  success: boolean;
  classes: IntelligenceClassRow[];
};

export function useIntelligenceClasses() {
  return useQuery({
    queryKey: ["intelligence-classes"],
    queryFn: async () => {
      // Auto-generated command routes are POST — call with an empty body.
      // No generated-SDK type exists for this endpoint yet (see Task 6
      // note) — legacyFetch is the documented exception, not a new
      // violation of "never call fetch directly".
      const res = await legacyFetch("/api/system/list-intelligence-classes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) {
        throw new Error(`API ${res.status}: ${await res.text()}`);
      }
      return (await res.json()) as IntelligenceClassesResponse;
    },
    staleTime: 30_000,
  });
}
```

(`useQuery` is already imported at the top of `hooks.ts`; `legacyFetch` is
not — the existing top-of-file import is `apiGet` only, so add the new
import line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/api/__tests__/useIntelligenceClasses.test.tsx`
Expected: `2 passed`.

- [ ] **Step 5: Migrate `IntelligenceClassPicker.tsx` to the hook**

Replace lines 1–63 (imports through the `useQuery` call) with:

```tsx
// dashboard/src/components/profile/IntelligenceClassPicker.tsx
import { useIntelligenceClasses, type IntelligenceClassRow as ClassRow } from "../../api/hooks";

const TIER_ORDER = ["fast", "standard", "deep"] as const;
const THINK_ORDER = ["off", "low", "medium", "high"] as const;

function tierOf(id: string): string {
  return id.split("-")[0] ?? id;
}
function thinkOf(id: string): string {
  return id.split("-")[1] ?? "";
}

function sortClasses(rows: ClassRow[]): ClassRow[] {
  return [...rows].sort((a, b) => {
    const ta = TIER_ORDER.indexOf(tierOf(a.id) as (typeof TIER_ORDER)[number]);
    const tb = TIER_ORDER.indexOf(tierOf(b.id) as (typeof TIER_ORDER)[number]);
    if (ta !== tb) return ta - tb;
    const ka = THINK_ORDER.indexOf(thinkOf(a.id) as (typeof THINK_ORDER)[number]);
    const kb = THINK_ORDER.indexOf(thinkOf(b.id) as (typeof THINK_ORDER)[number]);
    if (ka !== kb) return ka - kb;
    return a.id.localeCompare(b.id);
  });
}

interface Props {
  value: string;
  onChange: (next: string) => void;
}

export default function IntelligenceClassPicker({ value, onChange }: Props) {
  const { data, isLoading, error } = useIntelligenceClasses();
  const classes = sortClasses(data?.classes ?? []);
```

The remainder of the file (the `grouped`/`selected` derivations and the
JSX return, currently lines 65–127) is unchanged — only the fetch/query
plumbing above it changes; `data` now comes from the shared hook (a full
`IntelligenceClassesResponse`) instead of a bare array, so the `classes`
line above reads `data?.classes ?? []` instead of the old `data ?? []`.

- [ ] **Step 6: Migrate `IntelligenceClassesStub.tsx` to the hook**

Replace lines 1–72 (imports through the `useQuery` call) with:

```tsx
// dashboard/src/pages/settings/IntelligenceClassesStub.tsx
import { CpuChipIcon } from "@heroicons/react/24/outline";
import { useIntelligenceClasses, type IntelligenceClassRow } from "../../api/hooks";

type ProviderSlice = {
  model?: string;
  thinking?: string;
  reasoning_effort?: string;
  thinking_budget?: number;
  [k: string]: unknown;
};

const TIER_ORDER = ["fast", "standard", "deep"] as const;
const THINK_ORDER = ["off", "low", "medium", "high"] as const;

function tierOf(id: string): string {
  return id.split("-")[0] ?? id;
}
function thinkOf(id: string): string {
  return id.split("-")[1] ?? "";
}

function sortClasses(rows: IntelligenceClassRow[]): IntelligenceClassRow[] {
  return [...rows].sort((a, b) => {
    const ta = TIER_ORDER.indexOf(tierOf(a.id) as (typeof TIER_ORDER)[number]);
    const tb = TIER_ORDER.indexOf(tierOf(b.id) as (typeof TIER_ORDER)[number]);
    if (ta !== tb) return ta - tb;
    const ka = THINK_ORDER.indexOf(thinkOf(a.id) as (typeof THINK_ORDER)[number]);
    const kb = THINK_ORDER.indexOf(thinkOf(b.id) as (typeof THINK_ORDER)[number]);
    if (ka !== kb) return ka - kb;
    return a.id.localeCompare(b.id);
  });
}

function providerBadge(name: string, slice: ProviderSlice): string {
  const bits: string[] = [];
  if (slice.model) bits.push(slice.model);
  if (slice.thinking) bits.push(`think:${slice.thinking}`);
  if (slice.reasoning_effort) bits.push(`effort:${slice.reasoning_effort}`);
  if (typeof slice.thinking_budget === "number") bits.push(`budget:${slice.thinking_budget}`);
  return `${name}: ${bits.join(" · ") || "—"}`;
}

export default function IntelligenceClassesStub() {
  const { data, isLoading, error } = useIntelligenceClasses();
  const classes = sortClasses(data?.classes ?? []);
```

The remainder of the file (`grouped`, the loading/error/empty branches, and
the tiered card grid, currently lines 74–154) is unchanged.

- [ ] **Step 7: Typecheck + full suite**

Run: `cd dashboard && npm run typecheck && npm run test`
Expected: both exit 0 / all pass. (`ProviderSlice` is now imported from
`hooks.ts` in the picker but re-declared locally with an index signature in
the stub — deliberately kept distinct since the stub's index signature
usage differs; both compile against the same underlying `mapping` shape.)

- [ ] **Step 8: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/api/hooks.ts \
  src/components/profile/IntelligenceClassPicker.tsx \
  src/pages/settings/IntelligenceClassesStub.tsx \
  src/api/__tests__/useIntelligenceClasses.test.tsx
git commit -m "refactor(dashboard): consolidate intelligence-class fetches into useIntelligenceClasses"
```

---

## Task 7: Args schema — `dashboard/src/panes/contextual-settings/args.ts`

**Files:**
- Create: `dashboard/src/panes/contextual-settings/args.ts`
- Test: `dashboard/src/panes/contextual-settings/__tests__/args.test.ts`

**Interfaces:**
- Produces: `contextualSettingsArgsSchema: z.ZodDiscriminatedUnion` and
  `type ContextualSettingsArgs` — consumed by `manifest.ts` (Task 8),
  `index.tsx` (Task 9), and every subject component (Tasks 11–15).

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/contextual-settings/__tests__/args.test.ts
import { describe, expect, it } from "vitest";
import { contextualSettingsArgsSchema } from "../args";

describe("contextualSettingsArgsSchema", () => {
  it("accepts all five valid shapes", () => {
    const valid = [
      { subject: "project", subjectId: "demo" },
      { subject: "profile", subjectId: "reviewer" },
      { subject: "project-profile", subjectId: "coder", projectId: "demo" },
      { subject: "playbook", subjectId: "review-gate" },
      { subject: "intelligence-class", subjectId: "fast-off" },
    ];
    for (const v of valid) {
      expect(contextualSettingsArgsSchema.safeParse(v).success).toBe(true);
    }
  });

  it("rejects a project arg missing subjectId", () => {
    expect(contextualSettingsArgsSchema.safeParse({ subject: "project" }).success).toBe(false);
  });

  it("rejects an unknown subject", () => {
    expect(
      contextualSettingsArgsSchema.safeParse({ subject: "bogus", subjectId: "x" }).success,
    ).toBe(false);
  });

  it("rejects project-profile missing projectId", () => {
    expect(
      contextualSettingsArgsSchema.safeParse({ subject: "project-profile", subjectId: "x" })
        .success,
    ).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/args.test.ts`
Expected: FAIL — `Cannot find module '../args'`.

- [ ] **Step 3: Write `args.ts`** (verbatim from spec §4)

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
  subjectId: z.string().min(1), // agent_type
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
  projectArgs,
  profileArgs,
  projectProfileArgs,
  playbookArgs,
  intelligenceClassArgs,
]);
export type ContextualSettingsArgs = z.infer<typeof contextualSettingsArgsSchema>;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/args.test.ts`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/args.ts src/panes/contextual-settings/__tests__/args.test.ts
git commit -m "feat(dashboard): contextual-settings pane args schema"
```

---

## Task 8: Manifest — `dashboard/src/panes/contextual-settings/manifest.ts`

Per the plugin-interface spec's typing rule (Task 2), `open_shortcut` is
**omitted** rather than set to `null` — this plan's manifest deliberately
differs from the contextual-settings spec's own `open_shortcut: null`
example, resolving a contradiction between the two specs in favor of the
stricter (and depended-upon) plugin-interface contract.

**Files:**
- Create: `dashboard/src/panes/contextual-settings/manifest.ts`
- Test: `dashboard/src/panes/contextual-settings/__tests__/manifest.test.ts`

**Interfaces:**
- Consumes: `PaneManifest` from `../types` (Task 2),
  `contextualSettingsArgsSchema`/`ContextualSettingsArgs` from `./args`
  (Task 7).
- Produces: `export const manifest: PaneManifest<ContextualSettingsArgs>` —
  consumed by `registry.ts` (Task 10) and `registry.test.ts`.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/contextual-settings/__tests__/manifest.test.ts
import { describe, expect, it } from "vitest";
import { manifest } from "../manifest";

describe("contextual-settings manifest", () => {
  it("has id matching the directory name", () => {
    expect(manifest.id).toBe("contextual-settings");
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is cross-route and agent-pushable with a palette entry", () => {
    expect(manifest.route_scope).toBe("cross-route");
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.palette_label).toBe("Open settings for…");
    expect(manifest.palette_section).toBe("Settings");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/manifest.test.ts`
Expected: FAIL — `Cannot find module '../manifest'`.

- [ ] **Step 3: Write `manifest.ts`**

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
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open settings for…",
  palette_section: "Settings",
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/manifest.test.ts`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/manifest.ts src/panes/contextual-settings/__tests__/manifest.test.ts
git commit -m "feat(dashboard): contextual-settings pane manifest"
```

---

## Task 9: `useDirtyForm` hook

**Files:**
- Create: `dashboard/src/panes/contextual-settings/useDirtyForm.ts`
- Test: `dashboard/src/panes/contextual-settings/__tests__/useDirtyForm.test.ts`

**Interfaces:**
- Produces:
  ```ts
  function useDirtyForm<T>(initial: T): {
    value: T;
    setValue: Dispatch<SetStateAction<T>>;
    dirty: boolean;
    resetBaseline: (next: T) => void;
  }
  ```
  Consumed by `ProjectSubject`, `ProfileSubject`, `ProjectProfileSubject`,
  `PlaybookSubject` (Tasks 11, 12, 13, 14) — not `IntelligenceClassSubject`
  (read-only, Task 15).

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/contextual-settings/__tests__/useDirtyForm.test.ts
import { describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useDirtyForm } from "../useDirtyForm";

describe("useDirtyForm", () => {
  it("starts clean", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a" }));
    expect(result.current.dirty).toBe(false);
    expect(result.current.value).toEqual({ name: "a" });
  });

  it("becomes dirty when value diverges from baseline (deep comparison)", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a", tags: ["x"] }));
    act(() => {
      result.current.setValue({ name: "a", tags: ["x", "y"] });
    });
    expect(result.current.dirty).toBe(true);
  });

  it("resetBaseline clears dirty and adopts the new value", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a" }));
    act(() => {
      result.current.setValue({ name: "b" });
    });
    expect(result.current.dirty).toBe(true);
    act(() => {
      result.current.resetBaseline({ name: "c" });
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.value).toEqual({ name: "c" });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/useDirtyForm.test.ts`
Expected: FAIL — `Cannot find module '../useDirtyForm'`.

- [ ] **Step 3: Write `useDirtyForm.ts`**

Every consumer's form shape is a flat object of strings and string arrays
(`FormState`/`ProfileFormState`/a `draft: string`), so a small
JSON-serialization deep-equal is sufficient — no new dependency needed
(there is no existing `deepEqual` utility in the codebase to reuse, per
research; JSON comparison is the narrowest correct tool for these
JSON-serializable shapes and avoids adding one).

```ts
// dashboard/src/panes/contextual-settings/useDirtyForm.ts
import { useState } from "react";

export function useDirtyForm<T>(initial: T) {
  const [value, setValue] = useState<T>(initial);
  const [baseline, setBaseline] = useState<T>(initial);
  const dirty = JSON.stringify(value) !== JSON.stringify(baseline);
  const resetBaseline = (next: T) => {
    setValue(next);
    setBaseline(next);
  };
  return { value, setValue, dirty, resetBaseline };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/useDirtyForm.test.ts`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/useDirtyForm.ts \
  src/panes/contextual-settings/__tests__/useDirtyForm.test.ts
git commit -m "feat(dashboard): contextual-settings useDirtyForm hook"
```

---

## Task 10: Subject — `project` (`ProjectSubject.tsx`)

**Files:**
- Create: `dashboard/src/panes/contextual-settings/subjects/ProjectSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx`

**Interfaces:**
- Consumes: `useProject`, `useProjectProfiles`, `useEditProject` from
  `../../../api/hooks`; `FormState`, `parseOptionalInt`,
  `parseOptionalFloat`, `projectToForm`, `profileOptionsFromRows` from
  `../../../pages/project/Config` (Task 5); `useDirtyForm` (Task 9);
  `PaneViewProps`, `PaneToolbarAction` from `../../types` (Task 2);
  `Section`, `Field` from `../../../components/profile/FormSection` (Task
  3) — used here too, for a pane-width-appropriate stacked layout (a
  deliberate deviation from `Config.tsx`'s two-column `Row` grid, which
  doesn't fit a narrow pane).
- Produces: `export default function ProjectSubject(props: PaneViewProps<
  Extract<ContextualSettingsArgs, { subject: "project" }>>)`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import ProjectSubject from "../subjects/ProjectSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const project = {
  id: "demo",
  name: "Demo",
  repo_url: "git@github.com:org/demo.git",
  repo_default_branch: "main",
  default_profile_id: "",
  max_concurrent_agents: 2,
  credit_weight: 1,
  budget_limit: null,
  discord_channel_id: "",
  paused: false,
};

describe("ProjectSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useProject").mockReturnValue({
      data: project,
      isLoading: false,
      error: null,
    } as ReturnType<typeof hooks.useProject>);
    vi.spyOn(hooks, "useProjectProfiles").mockReturnValue({
      data: { agent_types: [] },
    } as unknown as ReturnType<typeof hooks.useProjectProfiles>);
  });

  it("renders repo_url read-only and enables Save once edited", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(project);
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    const setToolbar = vi.fn();
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={setToolbar}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("git@github.com:org/demo.git")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("git@github.com:org/demo.git")).not.toBeInTheDocument();

    const lastToolbarCall = () => setToolbar.mock.calls[setToolbar.mock.calls.length - 1][0];
    expect(lastToolbarCall().find((a: { id: string }) => a.id === "save").disabled).toBe(true);

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");

    await waitFor(() =>
      expect(lastToolbarCall().find((a: { id: string }) => a.id === "save").disabled).toBe(false),
    );
  });

  it("save payload matches Config.tsx's shape", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(project);
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(actions) => {
          toolbar = actions;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        project_id: "demo",
        name: "Demo v2",
        repo_default_branch: "main",
        default_profile_id: null,
        max_concurrent_agents: 2,
        credit_weight: 1,
        budget_limit: null,
        discord_channel_id: null,
      }),
    );
  });

  it("Discard changes reverts the form and re-disables Save", async () => {
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    let toolbar: { id: string; disabled?: boolean; onClick: () => void }[] = [];
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(actions) => {
          toolbar = actions;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");
    await waitFor(() => expect(toolbar.find((a) => a.id === "discard")!.disabled).toBe(false));

    toolbar.find((a) => a.id === "discard")!.onClick();

    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Demo"));
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")!.disabled).toBe(true));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx`
Expected: FAIL — `Cannot find module '../subjects/ProjectSubject'`.

- [ ] **Step 3: Write `ProjectSubject.tsx`**

```tsx
// dashboard/src/panes/contextual-settings/subjects/ProjectSubject.tsx
import { useEffect } from "react";
import { CheckIcon, ArrowUturnLeftIcon } from "@heroicons/react/24/outline";
import { useProject, useProjectProfiles, useEditProject } from "../../../api/hooks";
import {
  type FormState,
  parseOptionalInt,
  parseOptionalFloat,
  projectToForm,
  profileOptionsFromRows,
} from "../../../pages/project/Config";
import { Section, Field } from "../../../components/profile/FormSection";
import { useDirtyForm } from "../useDirtyForm";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "project" }>;

export default function ProjectSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data: project, isLoading, error } = useProject(args.subjectId);
  const { data: profiles } = useProjectProfiles(args.subjectId);
  const editProject = useEditProject();
  const { value: form, setValue: setForm, dirty, resetBaseline } = useDirtyForm<FormState>(
    projectToForm(project ?? {}),
  );

  useEffect(() => {
    if (project) resetBaseline(projectToForm(project));
    // resetBaseline is stable across renders (from useState setters); only
    // re-run when the server value actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);

  const save = async () => {
    if (!project) return;
    await editProject.mutateAsync({
      project_id: project.id,
      name: form.name.trim() || null,
      repo_default_branch: form.repo_default_branch.trim() || null,
      default_profile_id: form.default_profile_id.trim() || null,
      max_concurrent_agents: parseOptionalInt(form.max_concurrent_agents),
      credit_weight: parseOptionalFloat(form.credit_weight),
      budget_limit: parseOptionalFloat(form.budget_limit),
      discord_channel_id: form.discord_channel_id.trim() || null,
    });
    resetBaseline(form);
  };

  useEffect(() => {
    setToolbar([
      { id: "save", label: "Save", icon: CheckIcon, onClick: save, disabled: !dirty || editProject.isPending },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => project && resetBaseline(projectToForm(project)),
        disabled: !dirty,
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, editProject.isPending, form, project]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading project…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!project) return <p className="text-sm text-gray-500">Project not found.</p>;

  const profileOptions = profileOptionsFromRows(profiles?.agent_types ?? []);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="space-y-6 text-sm">
      <Section title="Basics">
        <Field label="Name">
          <input
            aria-label="Name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Repo URL">
          <span className="font-mono text-xs text-gray-400">{project.repo_url ?? "—"}</span>
        </Field>
        <Field label="Default branch">
          <input
            aria-label="Default branch"
            value={form.repo_default_branch}
            onChange={(e) => set("repo_default_branch", e.target.value)}
            placeholder="main"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Scheduling">
        <Field label="Default profile">
          <select
            aria-label="Default profile"
            value={form.default_profile_id}
            onChange={(e) => set("default_profile_id", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">— inherit / none —</option>
            {profileOptions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.id})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Max concurrent agents">
          <input
            aria-label="Max concurrent agents"
            type="number"
            min={1}
            value={form.max_concurrent_agents}
            onChange={(e) => set("max_concurrent_agents", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Budget">
        <Field label="Credit weight">
          <input
            aria-label="Credit weight"
            type="number"
            step={0.1}
            min={0}
            value={form.credit_weight}
            onChange={(e) => set("credit_weight", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Budget limit">
          <input
            aria-label="Budget limit"
            type="number"
            step={0.01}
            min={0}
            placeholder="(no limit)"
            value={form.budget_limit}
            onChange={(e) => set("budget_limit", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Discord">
        <Field label="Channel id">
          <input
            aria-label="Channel id"
            value={form.discord_channel_id}
            onChange={(e) => set("discord_channel_id", e.target.value)}
            placeholder="(channel id)"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      {editProject.isError && (
        <p className="text-sm text-red-400">
          {(editProject.error as Error)?.message ?? "Save failed."}
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx`
Expected: `3 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/subjects/ProjectSubject.tsx \
  src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings project subject"
```

---

## Task 11: Subject — `profile` (`ProfileSubject.tsx`)

**Files:**
- Create: `dashboard/src/panes/contextual-settings/subjects/ProfileSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx`

**Interfaces:**
- Consumes: `useGetProfile`, `useEditProfile` from `../../../api/hooks`;
  `profileToForm`, `ProfileFormState` from
  `../../../components/profile/profileForm` (Task 4); `Section`, `Field`
  from `../../../components/profile/FormSection` (Task 3);
  `IntelligenceClassPicker`, `McpServerSelector`, `ToolPicker` default
  exports from `../../../components/profile/*`; `useDirtyForm` (Task 9).
- Produces: `export default function ProfileSubject(props: PaneViewProps<
  Extract<ContextualSettingsArgs, { subject: "profile" }>>)`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import ProfileSubject from "../subjects/ProfileSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const profile = {
  id: "reviewer",
  name: "Reviewer",
  description: "Reviews PRs",
  default_class: "standard-medium",
  permission_mode: "acceptEdits",
  system_prompt_suffix: "Be terse.",
  allowed_tools: ["Read"],
  mcp_servers: [],
};

describe("ProfileSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useGetProfile").mockReturnValue({
      data: profile,
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useGetProfile>);
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: { success: true, classes: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
    vi.spyOn(hooks, "useMcpServers").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useMcpServers>);
    vi.spyOn(hooks, "useToolCatalog").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useToolCatalog>);
  });

  it("renders every drawer section", () => {
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("Basics")).toBeInTheDocument();
    expect(screen.getByText("Intelligence class & permissions")).toBeInTheDocument();
    expect(screen.getByText("System prompt suffix")).toBeInTheDocument();
    expect(screen.getByText("MCP servers")).toBeInTheDocument();
    expect(screen.getByText("Allowed tools")).toBeInTheDocument();
  });

  it("save payload matches SystemProfileEditDrawer's onSave shape", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(profile);
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.type(screen.getByLabelText("Name"), "!");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          profile_id: "reviewer",
          name: "Reviewer!",
          default_class: "standard-medium",
          mcp_servers: [],
        }),
      ),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx`
Expected: FAIL — `Cannot find module '../subjects/ProfileSubject'`.

- [ ] **Step 3: Write `ProfileSubject.tsx`**

```tsx
// dashboard/src/panes/contextual-settings/subjects/ProfileSubject.tsx
import { useEffect } from "react";
import { CheckIcon, ArrowUturnLeftIcon } from "@heroicons/react/24/outline";
import { useGetProfile, useEditProfile } from "../../../api/hooks";
import { profileToForm, type ProfileFormState as FormState } from "../../../components/profile/profileForm";
import { Section, Field } from "../../../components/profile/FormSection";
import IntelligenceClassPicker from "../../../components/profile/IntelligenceClassPicker";
import McpServerSelector from "../../../components/profile/McpServerSelector";
import ToolPicker from "../../../components/profile/ToolPicker";
import { useDirtyForm } from "../useDirtyForm";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "profile" }>;

export default function ProfileSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data: profile, isLoading } = useGetProfile(args.subjectId);
  const edit = useEditProfile();
  const { value: form, setValue: setForm, dirty, resetBaseline } = useDirtyForm<FormState>(
    profileToForm(profile),
  );

  useEffect(() => {
    if (profile) resetBaseline(profileToForm(profile));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const onMcpChange = (next: string[]) => {
    setForm((prev) => {
      const removed = prev.mcp_servers.filter((n) => !next.includes(n));
      if (removed.length === 0) return { ...prev, mcp_servers: next };
      const dropPrefixes = removed.map((n) => `mcp__${n}__`);
      const allowed = prev.allowed_tools.filter((t) => !dropPrefixes.some((p) => t.startsWith(p)));
      return { ...prev, mcp_servers: next, allowed_tools: allowed };
    });
  };

  const save = async () => {
    // Same stale-OpenAPI-shape casts as SystemProfileEditDrawer.onSave —
    // mcp_servers/default_class aren't on the generated request type yet;
    // the daemon accepts them (see _cmd_edit_profile).
    await edit.mutateAsync({
      profile_id: args.subjectId,
      name: form.name || null,
      description: form.description || null,
      default_class: form.default_class || "",
      permission_mode: form.permission_mode || null,
      system_prompt_suffix: form.system_prompt_suffix || null,
      allowed_tools: form.allowed_tools,
      mcp_servers: form.mcp_servers as unknown as Record<string, unknown>,
    } as unknown as Parameters<typeof edit.mutateAsync>[0]);
    resetBaseline(form);
  };

  useEffect(() => {
    setToolbar([
      { id: "save", label: "Save", icon: CheckIcon, onClick: save, disabled: !dirty || edit.isPending },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => profile && resetBaseline(profileToForm(profile)),
        disabled: !dirty,
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, edit.isPending, form, profile]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading profile…</p>;
  if (!profile) return <p className="text-sm text-gray-500">Profile not found.</p>;

  return (
    <div className="space-y-6 text-sm">
      <Section title="Basics">
        <Field label="Name">
          <input
            aria-label="Name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Description">
          <input
            aria-label="Description"
            value={form.description}
            onChange={(e) => set("description", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section
        title="Intelligence class & permissions"
        hint="Picks the model + reasoning tier per provider. See Settings → Intelligence Classes for the matrix."
      >
        <Field label="Intelligence class">
          <IntelligenceClassPicker value={form.default_class} onChange={(v) => set("default_class", v)} />
        </Field>
        <Field label="Permission mode">
          <input
            aria-label="Permission mode"
            value={form.permission_mode}
            onChange={(e) => set("permission_mode", e.target.value)}
            placeholder="acceptEdits"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="System prompt suffix">
        <textarea
          aria-label="System prompt suffix"
          value={form.system_prompt_suffix}
          onChange={(e) => set("system_prompt_suffix", e.target.value)}
          rows={5}
          className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
      </Section>

      <Section
        title="MCP servers"
        hint="Servers this profile may connect to. The embedded agent-queue server is always included."
      >
        <McpServerSelector value={form.mcp_servers} onChange={onMcpChange} />
      </Section>

      <Section title="Allowed tools" hint="Tools the agent may invoke. Groups appear for the servers selected above.">
        <ToolPicker value={form.allowed_tools} onChange={(t) => set("allowed_tools", t)} enabledServers={form.mcp_servers} model="" />
      </Section>

      {edit.isError && (
        <p className="text-sm text-red-400">{(edit.error as Error)?.message ?? "Save failed."}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx`
Expected: `2 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/subjects/ProfileSubject.tsx \
  src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings profile subject"
```

---

## Task 12: Subject — `project-profile` (`ProjectProfileSubject.tsx`)

**Files:**
- Create: `dashboard/src/panes/contextual-settings/subjects/ProjectProfileSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx`

**Interfaces:**
- Consumes: `useProjectProfiles`, `useEditProjectProfile` from
  `../../../api/hooks`; `profileToForm`, `ProfileFormState` (Task 4);
  `Section`/`Field` (Task 3); the same three picker components as Task 11;
  `useDirtyForm` (Task 9).
- Produces: `export default function ProjectProfileSubject(props:
  PaneViewProps<Extract<ContextualSettingsArgs, { subject:
  "project-profile" }>>)`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import ProjectProfileSubject from "../subjects/ProjectProfileSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("ProjectProfileSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: { success: true, classes: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
    vi.spyOn(hooks, "useMcpServers").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useMcpServers>);
    vi.spyOn(hooks, "useToolCatalog").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useToolCatalog>);
    vi.spyOn(hooks, "useEditProjectProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProjectProfile>);
  });

  it("seeds from scoped when present", () => {
    vi.spyOn(hooks, "useProjectProfiles").mockReturnValue({
      data: {
        agent_types: [
          {
            agent_type: "coder",
            scoped: { name: "Coder (demo)", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
            global: { name: "Coder", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
          },
        ],
      },
    } as unknown as ReturnType<typeof hooks.useProjectProfiles>);

    render(
      <ProjectProfileSubject
        args={{ subject: "project-profile", subjectId: "coder", projectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Name")).toHaveValue("Coder (demo)");
  });

  it("falls back to global and disables Save when no scoped override exists", () => {
    vi.spyOn(hooks, "useProjectProfiles").mockReturnValue({
      data: {
        agent_types: [
          {
            agent_type: "coder",
            scoped: null,
            global: { name: "Coder", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
          },
        ],
      },
    } as unknown as ReturnType<typeof hooks.useProjectProfiles>);

    let toolbar: { id: string; disabled?: boolean }[] = [];
    render(
      <ProjectProfileSubject
        args={{ subject: "project-profile", subjectId: "coder", projectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Name")).toHaveValue("Coder");
    expect(screen.getByText(/No project override exists yet/)).toBeInTheDocument();
    expect(toolbar.find((a) => a.id === "save")!.disabled).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx`
Expected: FAIL — `Cannot find module '../subjects/ProjectProfileSubject'`.

- [ ] **Step 3: Write `ProjectProfileSubject.tsx`**

```tsx
// dashboard/src/panes/contextual-settings/subjects/ProjectProfileSubject.tsx
import { useEffect } from "react";
import { CheckIcon, ArrowUturnLeftIcon } from "@heroicons/react/24/outline";
import { useProjectProfiles, useEditProjectProfile } from "../../../api/hooks";
import { profileToForm, type ProfileFormState as FormState } from "../../../components/profile/profileForm";
import { Section, Field } from "../../../components/profile/FormSection";
import IntelligenceClassPicker from "../../../components/profile/IntelligenceClassPicker";
import McpServerSelector from "../../../components/profile/McpServerSelector";
import ToolPicker from "../../../components/profile/ToolPicker";
import { useDirtyForm } from "../useDirtyForm";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "project-profile" }>;

export default function ProjectProfileSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data: rows } = useProjectProfiles(args.projectId);
  const row = rows?.agent_types?.find((r) => r.agent_type === args.subjectId);
  const scoped = row?.scoped ?? null;
  const global = row?.global ?? null;
  const seed = scoped ?? global;

  const edit = useEditProjectProfile();
  const { value: form, setValue: setForm, dirty, resetBaseline } = useDirtyForm<FormState>(
    profileToForm(seed),
  );

  useEffect(() => {
    if (seed) resetBaseline(profileToForm(seed));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const onMcpChange = (next: string[]) => {
    setForm((prev) => {
      const removed = prev.mcp_servers.filter((n) => !next.includes(n));
      if (removed.length === 0) return { ...prev, mcp_servers: next };
      const dropPrefixes = removed.map((n) => `mcp__${n}__`);
      const allowed = prev.allowed_tools.filter((t) => !dropPrefixes.some((p) => t.startsWith(p)));
      return { ...prev, mcp_servers: next, allowed_tools: allowed };
    });
  };

  const save = async () => {
    await edit.mutateAsync({
      project_id: args.projectId,
      agent_type: args.subjectId,
      name: form.name || null,
      description: form.description || null,
      default_class: form.default_class || "",
      permission_mode: form.permission_mode || null,
      system_prompt_suffix: form.system_prompt_suffix || null,
      allowed_tools: form.allowed_tools,
      mcp_servers: form.mcp_servers,
    } as unknown as Parameters<typeof edit.mutateAsync>[0]);
    resetBaseline(form);
  };

  useEffect(() => {
    setToolbar([
      {
        id: "save",
        label: "Save",
        icon: CheckIcon,
        onClick: save,
        // No "create override" flow exists in ProfileEditDrawer.tsx either
        // (spec §5.3/§13) — Save stays disabled with no scoped row.
        disabled: !scoped || !dirty || edit.isPending,
      },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => seed && resetBaseline(profileToForm(seed)),
        disabled: !dirty,
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, edit.isPending, form, scoped, seed]);

  if (!row) return <p className="text-sm text-gray-500">Loading profile…</p>;

  return (
    <div className="space-y-6 text-sm">
      {!scoped && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          No project override exists yet.
        </p>
      )}

      <Section title="Basics">
        <Field label="Name">
          <input
            aria-label="Name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Description">
          <input
            aria-label="Description"
            value={form.description}
            onChange={(e) => set("description", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section
        title="Intelligence class & permissions"
        hint="Picks the model + reasoning tier per provider. See Settings → Intelligence Classes for the matrix."
      >
        <Field label="Intelligence class">
          <IntelligenceClassPicker value={form.default_class} onChange={(v) => set("default_class", v)} />
        </Field>
        <Field label="Permission mode">
          <input
            aria-label="Permission mode"
            value={form.permission_mode}
            onChange={(e) => set("permission_mode", e.target.value)}
            placeholder="acceptEdits"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="System prompt suffix">
        <textarea
          aria-label="System prompt suffix"
          value={form.system_prompt_suffix}
          onChange={(e) => set("system_prompt_suffix", e.target.value)}
          rows={5}
          className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
      </Section>

      <Section
        title="MCP servers"
        hint="Servers this profile may connect to. The embedded agent-queue server is always included."
      >
        <McpServerSelector projectId={args.projectId} value={form.mcp_servers} onChange={onMcpChange} />
      </Section>

      <Section title="Allowed tools" hint="Tools the agent may invoke. Groups appear for the servers selected above.">
        <ToolPicker
          projectId={args.projectId}
          value={form.allowed_tools}
          onChange={(t) => set("allowed_tools", t)}
          enabledServers={form.mcp_servers}
          model=""
        />
      </Section>

      {edit.isError && (
        <p className="text-sm text-red-400">{(edit.error as Error)?.message ?? "Save failed."}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx`
Expected: `2 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/subjects/ProjectProfileSubject.tsx \
  src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings project-profile subject"
```

---

## Task 13: Subject — `playbook` (`PlaybookSubject.tsx`)

**Files:**
- Create: `dashboard/src/panes/contextual-settings/subjects/PlaybookSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx`

**Interfaces:**
- Consumes: `usePlaybookSource`, `useUpdatePlaybookSource`, `usePlaybooks`
  from `../../../api/hooks`.
- Produces: `export default function PlaybookSubject(props: PaneViewProps<
  Extract<ContextualSettingsArgs, { subject: "playbook" }>>)`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import PlaybookSubject from "../subjects/PlaybookSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const source = { path: "vault/playbooks/review-gate.md", markdown: "# review-gate\n", source_hash: "abc123" };

describe("PlaybookSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "usePlaybookSource").mockReturnValue({
      data: source,
      isLoading: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof hooks.usePlaybookSource>);
    vi.spyOn(hooks, "usePlaybooks").mockReturnValue({
      data: [{ id: "review-gate", scope: "system", version: 1, node_count: 3, triggers: ["task.closed"] }],
    } as unknown as ReturnType<typeof hooks.usePlaybooks>);
  });

  it("renders the textarea seeded from source.markdown", () => {
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Playbook source")).toHaveValue("# review-gate\n");
  });

  it("save calls useUpdatePlaybookSource with the loaded hash", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ compiled: true, version: 2, node_count: 3, source_hash: "def456" });
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.type(screen.getByLabelText("Playbook source"), "\n# more");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        playbook_id: "review-gate",
        markdown: "# review-gate\n\n# more",
        expected_source_hash: "abc123",
      }),
    );
  });

  it("a conflict response surfaces without clobbering the draft", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ compiled: false, error: "conflict", errors: null });
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.type(screen.getByLabelText("Playbook source"), "!");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() => expect(screen.getByText(/Vault changed underneath this editor/)).toBeInTheDocument());
    expect(screen.getByLabelText("Playbook source")).toHaveValue("# review-gate\n!");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx`
Expected: FAIL — `Cannot find module '../subjects/PlaybookSubject'`.

- [ ] **Step 3: Write `PlaybookSubject.tsx`**

```tsx
// dashboard/src/panes/contextual-settings/subjects/PlaybookSubject.tsx
import { useEffect, useState } from "react";
import { CheckIcon, ArrowUturnLeftIcon } from "@heroicons/react/24/outline";
import { usePlaybookSource, useUpdatePlaybookSource, usePlaybooks, type PlaybookUpdateResult } from "../../../api/hooks";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "playbook" }>;

export default function PlaybookSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data: source, isLoading } = usePlaybookSource(args.subjectId);
  const { data: playbooks } = usePlaybooks();
  const update = useUpdatePlaybookSource();
  const meta = playbooks?.find((p) => p.id === args.subjectId);

  const [draft, setDraft] = useState("");
  const [baseHash, setBaseHash] = useState("");
  const [lastResult, setLastResult] = useState<PlaybookUpdateResult | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (source) {
      setDraft(source.markdown);
      setBaseHash(source.source_hash);
      setLastResult(null);
      setSaveError(null);
    }
  }, [source]);

  const dirty = source ? draft !== source.markdown : false;

  const save = async () => {
    setSaveError(null);
    setLastResult(null);
    try {
      const result = await update.mutateAsync({
        playbook_id: args.subjectId,
        markdown: draft,
        expected_source_hash: baseHash,
      });
      setLastResult(result);
      if (result.source_hash) setBaseHash(result.source_hash);
      if (result.error === "conflict") {
        setSaveError(
          "Vault changed underneath this editor. Reload to pick up the latest, or overwrite by saving again without the hash.",
        );
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    setToolbar([
      { id: "save", label: "Save", icon: CheckIcon, onClick: save, disabled: !dirty || update.isPending },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => source && setDraft(source.markdown),
        disabled: !dirty,
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, update.isPending, draft, source]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading source…</p>;
  if (!source) return <p className="text-sm text-gray-500">Source unavailable.</p>;

  return (
    <div className="space-y-3 text-sm">
      {meta && (
        <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
          <span>{meta.scope}{meta.scope_identifier ? `:${meta.scope_identifier}` : ""}</span>
          <span>v{meta.version}</span>
          <span>{meta.node_count} nodes</span>
          {(meta.triggers ?? []).map((t) => (
            <span key={t} className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">{t}</span>
          ))}
        </div>
      )}

      <textarea
        aria-label="Playbook source"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        className="h-[50vh] w-full resize-none rounded-lg border border-gray-800 bg-gray-900 p-3 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
      />

      {saveError && <p className="text-xs text-red-400">{saveError}</p>}

      {lastResult && lastResult.compiled && (
        <p className="text-xs text-emerald-300">
          Compiled v{lastResult.version} — {lastResult.node_count} nodes.
        </p>
      )}

      {lastResult && !lastResult.compiled && lastResult.errors && (
        <div className="text-xs text-amber-200">
          <p>Validation failed — previous compiled version still live.</p>
          <ul className="ml-4 list-disc">
            {lastResult.errors.map((e, i) => (
              <li key={i} className="font-mono">{e}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx`
Expected: `3 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/subjects/PlaybookSubject.tsx \
  src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings playbook subject"
```

---

## Task 14: Subject — `intelligence-class` (`IntelligenceClassSubject.tsx`)

Read-only — no save/discard, no dirty tracking.

**Files:**
- Create: `dashboard/src/panes/contextual-settings/subjects/IntelligenceClassSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/IntelligenceClassSubject.test.tsx`

**Interfaces:**
- Consumes: `useIntelligenceClasses` from `../../../api/hooks` (Task 6).
- Produces: `export default function IntelligenceClassSubject(props:
  PaneViewProps<Extract<ContextualSettingsArgs, { subject:
  "intelligence-class" }>>)`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/IntelligenceClassSubject.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import IntelligenceClassSubject from "../subjects/IntelligenceClassSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("IntelligenceClassSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: {
        success: true,
        classes: [
          { id: "fast-off", name: "Fast", description: "Quick, cheap.", mapping: { anthropic: { model: "haiku" } } },
          { id: "deep-high", name: "Deep", description: "Slow, thorough.", mapping: {} },
        ],
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
  });

  it("renders only the matching class, the vault hint, and only an open-full toolbar action", () => {
    let toolbar: { id: string }[] = [];
    render(
      <IntelligenceClassSubject
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("Fast")).toBeInTheDocument();
    expect(screen.queryByText("Deep")).not.toBeInTheDocument();
    expect(screen.getByText(/anthropic/)).toBeInTheDocument();
    expect(screen.getByText(/Edit/)).toBeInTheDocument();
    expect(toolbar).toEqual([{ id: "open-full", label: "Open full settings page", icon: expect.anything(), onClick: expect.any(Function) }]);
  });

  it("renders a not-found message for an id absent from the list", () => {
    render(
      <IntelligenceClassSubject
        args={{ subject: "intelligence-class", subjectId: "missing" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText('Intelligence class "missing" not found.')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/IntelligenceClassSubject.test.tsx`
Expected: FAIL — `Cannot find module '../subjects/IntelligenceClassSubject'`.

- [ ] **Step 3: Write `IntelligenceClassSubject.tsx`**

```tsx
// dashboard/src/panes/contextual-settings/subjects/IntelligenceClassSubject.tsx
import { useEffect } from "react";
import { ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useIntelligenceClasses } from "../../../api/hooks";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "intelligence-class" }>;

export default function IntelligenceClassSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data, isLoading, error } = useIntelligenceClasses();
  const navigate = useNavigate();
  const cls = data?.classes.find((c) => c.id === args.subjectId);

  useEffect(() => {
    setToolbar([
      {
        id: "open-full",
        label: "Open full settings page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: () => navigate("/settings/intelligence-classes"),
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (isLoading) return <p className="text-sm text-gray-500">Loading intelligence class…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!cls) return <p className="text-sm text-gray-500">Intelligence class "{args.subjectId}" not found.</p>;

  return (
    <div className="space-y-3 text-sm">
      <div>
        <p className="font-medium text-gray-100">{cls.name}</p>
        <p className="text-xs text-gray-400">{cls.description}</p>
      </div>
      <ul className="space-y-1 font-mono text-xs text-gray-400">
        {Object.entries(cls.mapping)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([provider, slice]) => (
            <li key={provider}>
              <span className="text-gray-500">{provider}:</span> {slice.model ?? "—"}
              {slice.thinking ? ` · think:${slice.thinking}` : ""}
            </li>
          ))}
      </ul>
      <p className="text-xs text-gray-500">
        These classes ship from the vault. Edit{" "}
        <code className="rounded bg-gray-800 px-1">vault/intelligence-classes/{args.subjectId}.md</code>{" "}
        to change them.
      </p>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/IntelligenceClassSubject.test.tsx`
Expected: `2 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/subjects/IntelligenceClassSubject.tsx \
  src/panes/contextual-settings/__tests__/IntelligenceClassSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings intelligence-class subject"
```

---

## Task 15: `index.tsx` subject switch + `$mod-s`/`Esc` shortcuts

**Files:**
- Create: `dashboard/src/panes/contextual-settings/index.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: all five subject components (Tasks 10–14), `PaneViewProps`
  (Task 2), `ContextualSettingsArgs` (Task 7).
- Produces: `export default function ContextualSettingsPane(props:
  PaneViewProps<ContextualSettingsArgs>)` — the component `registry.ts`
  (Task 16) wires to `manifest.id === "contextual-settings"`.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/contextual-settings/__tests__/index.test.tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import ContextualSettingsPane from "../index";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("ContextualSettingsPane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: { success: true, classes: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
  });

  it("dispatches to IntelligenceClassSubject for subject: intelligence-class", () => {
    render(
      <ContextualSettingsPane
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );
    expect(screen.getByText(/not found/)).toBeInTheDocument();
  });

  it("registers $mod-s and Escape shortcuts for editable subjects", () => {
    vi.spyOn(hooks, "usePlaybookSource").mockReturnValue({
      data: { path: "x.md", markdown: "hi", source_hash: "h" },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.usePlaybookSource>);
    vi.spyOn(hooks, "usePlaybooks").mockReturnValue({ data: [] } as unknown as ReturnType<typeof hooks.usePlaybooks>);
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    const setShortcuts = vi.fn();
    render(
      <ContextualSettingsPane
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={setShortcuts}
      />,
      { wrapper },
    );

    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1][0];
    expect(bindings.map((b: { key: string }) => b.key)).toEqual(["$mod-s", "Escape"]);
  });

  it("does not register $mod-s for the read-only intelligence-class subject", () => {
    const setShortcuts = vi.fn();
    render(
      <ContextualSettingsPane
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={setShortcuts}
      />,
      { wrapper },
    );

    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0] ?? [];
    expect(bindings.find((b: { key: string }) => b.key === "$mod-s")).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/index.test.tsx`
Expected: FAIL — `Cannot find module '../index'`.

- [ ] **Step 3: Write `index.tsx`**

Shortcuts live at the switch level (not duplicated per-subject) since
`Esc`'s confirm-discard behavior is identical for every editable subject
and only needs to know "is this subject dirty" — which none of the five
subject components currently expose upward. Rather than plumb a dirty
callback out of each subject (a larger API change touching all five
already-committed files), this task tracks dirtiness at the switch level
the same way the toolbar's `disabled` flags already do: by re-deriving it
from each subject's own `useDirtyForm`-backed toolbar registration is not
visible here, so `index.tsx` instead owns a **second**, wrapper-level
`useDirtyForm`-style boolean fed by a `onDirtyChange` prop... — no: to keep
every subject's already-tested internal state as the single source of
truth and avoid a second parallel dirty tracker that could drift from it,
`Esc`'s confirm check reads the *toolbar* actions each subject already
registers: the `discard` action's `disabled` flag is `!dirty` in every
editable subject (Tasks 10–13), so `index.tsx` treats "toolbar has a
`discard` action that is not disabled" as its dirty signal — no new prop,
no duplicated state.

```tsx
// dashboard/src/panes/contextual-settings/index.tsx
import { useEffect, useRef } from "react";
import type { PaneViewProps, PaneToolbarAction } from "../types";
import type { ContextualSettingsArgs } from "./args";
import ProjectSubject from "./subjects/ProjectSubject";
import ProfileSubject from "./subjects/ProfileSubject";
import ProjectProfileSubject from "./subjects/ProjectProfileSubject";
import PlaybookSubject from "./subjects/PlaybookSubject";
import IntelligenceClassSubject from "./subjects/IntelligenceClassSubject";

export default function ContextualSettingsPane(props: PaneViewProps<ContextualSettingsArgs>) {
  const { args, close, setToolbar, setShortcuts } = props;
  const toolbarRef = useRef<PaneToolbarAction[]>([]);

  const wrappedSetToolbar = (actions: PaneToolbarAction[]) => {
    toolbarRef.current = actions;
    setToolbar(actions);
  };

  useEffect(() => {
    const isDirty = () => {
      const discard = toolbarRef.current.find((a) => a.id === "discard");
      return discard ? !discard.disabled : false;
    };
    const save = () => {
      const saveAction = toolbarRef.current.find((a) => a.id === "save");
      if (saveAction && !saveAction.disabled) saveAction.onClick();
    };
    const handleEscape = () => {
      if (!isDirty()) {
        close();
        return;
      }
      if (window.confirm("Discard unsaved changes to this settings pane?")) close();
    };

    if (args.subject === "intelligence-class") {
      setShortcuts([]);
      return;
    }
    setShortcuts([
      { key: "$mod-s", label: "Save", onFire: save },
      { key: "Escape", label: "Discard & close", onFire: handleEscape },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [args.subject, close]);

  switch (args.subject) {
    case "project":
      return <ProjectSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "profile":
      return <ProfileSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "project-profile":
      return <ProjectProfileSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "playbook":
      return <PlaybookSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "intelligence-class":
      return <IntelligenceClassSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    default: {
      const _exhaustive: never = args;
      return _exhaustive;
    }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/index.test.tsx`
Expected: `3 passed`.

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/index.tsx src/panes/contextual-settings/__tests__/index.test.tsx
git commit -m "feat(dashboard): contextual-settings subject switch + save/discard shortcuts"
```

---

## Task 16: Frontend registry — `dashboard/src/panes/registry.ts`

**Files:**
- Create: `dashboard/src/panes/registry.ts`
- Test: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Consumes: `PaneEntry` (Task 2), `manifest` (Task 8), default export of
  `./contextual-settings` (Task 15).
- Produces: `export const PANE_REGISTRY: Record<string, PaneEntry>` —
  consumed by Task 17's Python parity test (indirectly, by scanning the
  same directory) and by any future shell integration.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/__tests__/registry.test.ts
import { describe, expect, it } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("contains contextual-settings, resolvable and matching its own id", () => {
    const entry = PANE_REGISTRY["contextual-settings"];
    expect(entry).toBeDefined();
    expect(entry.manifest.id).toBe("contextual-settings");
    expect(entry.Component).toBeTypeOf("function");
  });

  it("every entry's manifest.id matches its registry key", () => {
    for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(key);
    }
  });

  it("has no open_shortcut collisions", () => {
    const shortcuts = Object.values(PANE_REGISTRY)
      .map((e) => e.manifest.open_shortcut)
      .filter((s): s is string => !!s);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: FAIL — `Cannot find module '../registry'`.

- [ ] **Step 3: Write `registry.ts`**

Uses `import.meta.glob` per the plugin-interface spec §4.1, even though
only one view directory exists today — this is the intended growth point
for the eight other pane views the shell spec lists, none of which are in
scope for this plan.

```ts
// dashboard/src/panes/registry.ts
import type { ComponentType } from "react";
import type { PaneEntry, PaneManifest, PaneViewProps } from "./types";

const manifestModules = import.meta.glob("./*/manifest.ts", { eager: true }) as Record<
  string,
  { manifest: PaneManifest }
>;
const componentModules = import.meta.glob("./*/index.tsx", { eager: true }) as Record<
  string,
  { default: ComponentType<PaneViewProps> }
>;

function dirOf(path: string): string {
  // "./contextual-settings/manifest.ts" -> "contextual-settings"
  const match = /^\.\/([^/]+)\//.exec(path);
  if (!match) throw new Error(`pane registry: unexpected module path "${path}"`);
  return match[1];
}

const registry: Record<string, PaneEntry> = {};
const seenShortcuts = new Set<string>();

for (const [path, mod] of Object.entries(manifestModules)) {
  const dir = dirOf(path);
  const manifest = mod.manifest;
  if (manifest.id !== dir) {
    throw new Error(`pane registry: manifest.id "${manifest.id}" does not match directory "${dir}"`);
  }
  if (registry[manifest.id]) {
    throw new Error(`pane registry: duplicate id "${manifest.id}"`);
  }
  if (manifest.open_shortcut) {
    if (seenShortcuts.has(manifest.open_shortcut)) {
      throw new Error(`pane registry: open_shortcut collision on "${manifest.open_shortcut}"`);
    }
    seenShortcuts.add(manifest.open_shortcut);
  }

  const componentPath = `./${dir}/index.tsx`;
  const componentMod = componentModules[componentPath];
  if (!componentMod?.default) {
    throw new Error(`pane registry: "${dir}" has a manifest but no default component export`);
  }

  registry[manifest.id] = { manifest, Component: componentMod.default };
}

export const PANE_REGISTRY: Record<string, PaneEntry> = registry;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: `3 passed`.

- [ ] **Step 5: Typecheck + full suite**

Run: `cd dashboard && npm run typecheck && npm run test`
Expected: both exit 0 / all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/registry.ts src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): pane registry (build-time glob assembly)"
```

---

## Task 17: Server-side registry — `src/panes/registry.py` + parity test

Neither `src/panes/` nor `tests/test_pane_registry_parity.py` exists in this
repo. Create both — a minimal Python mirror of the frontend registry
(currently one entry) plus the parity test the plugin-interface spec §7
calls for. The parity test scans `dashboard/src/panes/*/manifest.ts`
textually for an `id: "..."` literal rather than executing TypeScript — no
Node/JS runtime dependency needed from pytest.

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Create: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict[str, bool]]` in
  `src/panes/registry.py`, keyed by pane view id, valued
  `{"agent_pushable": bool}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pane_registry_parity.py
import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]
PANES_DIR = REPO_ROOT / "dashboard" / "src" / "panes"


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    for manifest_path in PANES_DIR.glob("*/manifest.ts"):
        text = manifest_path.read_text()
        match = re.search(r'id:\s*"([^"]+)"', text)
        assert match, f"no `id: \"...\"` literal found in {manifest_path}"
        ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids


def test_contextual_settings_is_agent_pushable():
    assert SERVER_PANE_REGISTRY["contextual-settings"]["agent_pushable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.panes'`.

- [ ] **Step 3: Write `src/panes/__init__.py` and `src/panes/registry.py`**

```python
# src/panes/__init__.py
```

```python
# src/panes/registry.py
"""Static mirror of the frontend pane view registry (dashboard/src/panes/).

Kept in sync by hand — see tests/test_pane_registry_parity.py, which fails
CI if this dict and the frontend manifests' `id` fields diverge. Per
docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7,
option A ("static list") was chosen over generating this file, since the
view count is small enough that hand-sync is cheap.
"""

SERVER_PANE_REGISTRY: dict[str, dict[str, bool]] = {
    "contextual-settings": {"agent_pushable": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/panes/__init__.py src/panes/registry.py tests/test_pane_registry_parity.py
git commit -m "feat: server-side pane registry mirror + frontend/backend parity test"
```

---

## Task 18: `fullSettingsRoute` + `[Open full settings page]` toolbar action

The four editable subjects already register `save`/`discard` toolbar
actions (Tasks 10–13); `open-full` is the third toolbar entry every subject
needs per spec §6.1/§6.2, plus a standalone exported route-resolution
function so it's independently testable against the spec's routing table.

**Files:**
- Create: `dashboard/src/panes/contextual-settings/fullSettingsRoute.ts`
- Modify: `dashboard/src/panes/contextual-settings/subjects/ProjectSubject.tsx`
- Modify: `dashboard/src/panes/contextual-settings/subjects/ProfileSubject.tsx`
- Modify: `dashboard/src/panes/contextual-settings/subjects/ProjectProfileSubject.tsx`
- Modify: `dashboard/src/panes/contextual-settings/subjects/PlaybookSubject.tsx`
- Test: `dashboard/src/panes/contextual-settings/__tests__/fullSettingsRoute.test.ts`

**Interfaces:**
- Produces: `function fullSettingsRoute(args: ContextualSettingsArgs):
  string` — a pure function, importable without a router context, used by
  all five subjects' `open-full` toolbar action.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/contextual-settings/__tests__/fullSettingsRoute.test.ts
import { describe, expect, it } from "vitest";
import { fullSettingsRoute } from "../fullSettingsRoute";

describe("fullSettingsRoute", () => {
  it.each([
    [{ subject: "project", subjectId: "demo" } as const, "/projects/demo/config"],
    [{ subject: "profile", subjectId: "reviewer" } as const, "/settings/profiles"],
    [
      { subject: "project-profile", subjectId: "coder", projectId: "demo" } as const,
      "/projects/demo/profiles",
    ],
    [{ subject: "playbook", subjectId: "review-gate" } as const, "/playbooks/review-gate"],
    [
      { subject: "intelligence-class", subjectId: "fast-off" } as const,
      "/settings/intelligence-classes",
    ],
  ])("resolves %o to %s", (args, expected) => {
    expect(fullSettingsRoute(args)).toBe(expected);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/fullSettingsRoute.test.ts`
Expected: FAIL — `Cannot find module '../fullSettingsRoute'`.

- [ ] **Step 3: Write `fullSettingsRoute.ts`**

```ts
// dashboard/src/panes/contextual-settings/fullSettingsRoute.ts
import type { ContextualSettingsArgs } from "./args";

/**
 * Per spec §6.2: none of the four editable subjects have a routed detail
 * page keyed by id. Three of five land on a list, not the specific item —
 * see the spec's §13 open question for the v2 follow-up (deep-linking).
 */
export function fullSettingsRoute(args: ContextualSettingsArgs): string {
  switch (args.subject) {
    case "project":
      return `/projects/${args.subjectId}/config`;
    case "profile":
      return "/settings/profiles";
    case "project-profile":
      return `/projects/${args.projectId}/profiles`;
    case "playbook":
      return `/playbooks/${args.subjectId}`;
    case "intelligence-class":
      return "/settings/intelligence-classes";
    default: {
      const _exhaustive: never = args;
      return _exhaustive;
    }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings/__tests__/fullSettingsRoute.test.ts`
Expected: `5 passed`.

- [ ] **Step 5: Wire the `open-full` toolbar action into the four editable subjects**

In each of `ProjectSubject.tsx`, `ProfileSubject.tsx`,
`ProjectProfileSubject.tsx`, `PlaybookSubject.tsx`: add the import

```tsx
import { ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { fullSettingsRoute } from "../fullSettingsRoute";
```

add `const navigate = useNavigate();` alongside each component's existing
hook calls, and append a third entry to each `setToolbar([...])` array
(the two existing `save`/`discard` entries are unchanged):

```tsx
{
  id: "open-full",
  label: "Open full settings page",
  icon: ArrowTopRightOnSquareIcon,
  onClick: () => navigate(fullSettingsRoute(args)),
},
```

- [ ] **Step 6: Update each subject's toolbar test to assert the third action exists**

Add one assertion to the "renders" test in each of
`ProjectSubject.test.tsx`, `ProfileSubject.test.tsx`,
`ProjectProfileSubject.test.tsx`, `PlaybookSubject.test.tsx` (each already
captures `toolbar` via a `setToolbar` mock — add, e.g., in
`ProjectSubject.test.tsx`'s first test, after the existing `Save` disabled
assertion):

```ts
expect(lastToolbarCall().map((a: { id: string }) => a.id)).toEqual(["save", "discard", "open-full"]);
```

(mirror this line — with the appropriate `toolbar`/`lastToolbarCall`
accessor already present in that file — in the other three subject test
files' first `it(...)` block.)

- [ ] **Step 7: Run every subject test file + typecheck**

Run: `cd dashboard && npx vitest run src/panes/contextual-settings && npm run typecheck`
Expected: all pass, exits 0.

- [ ] **Step 8: Commit**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
git add src/panes/contextual-settings/fullSettingsRoute.ts \
  src/panes/contextual-settings/__tests__/fullSettingsRoute.test.ts \
  src/panes/contextual-settings/subjects/ProjectSubject.tsx \
  src/panes/contextual-settings/subjects/ProfileSubject.tsx \
  src/panes/contextual-settings/subjects/ProjectProfileSubject.tsx \
  src/panes/contextual-settings/subjects/PlaybookSubject.tsx \
  src/panes/contextual-settings/__tests__/ProjectSubject.test.tsx \
  src/panes/contextual-settings/__tests__/ProfileSubject.test.tsx \
  src/panes/contextual-settings/__tests__/ProjectProfileSubject.test.tsx \
  src/panes/contextual-settings/__tests__/PlaybookSubject.test.tsx
git commit -m "feat(dashboard): contextual-settings 'Open full settings page' toolbar action"
```

---

## Task 19: Full suite verification + manual checklist

Final task — no new source files, just end-to-end verification and the
manual checklist the spec's non-goals (no E2E infra) require in place of
automated cross-page tests.

**Files:** none created or modified.

- [ ] **Step 1: Run the full frontend suite**

Run: `cd dashboard && npm run test`
Expected: every test file from Tasks 1–18 passes (types, FormSection,
profileForm, Config helpers, useIntelligenceClasses, args, manifest,
useDirtyForm, all five subjects, index, registry, fullSettingsRoute).

- [ ] **Step 2: Run typecheck and lint**

Run: `cd dashboard && npm run typecheck && npm run lint`
Expected: both exit 0.

- [ ] **Step 3: Run the build**

Run: `cd dashboard && npm run build`
Expected: exits 0 (confirms `import.meta.glob` resolves correctly under
Vite's production build, not just dev/test).

- [ ] **Step 4: Run the backend parity test plus the full backend suite**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v && pytest tests/ -n auto`
Expected: parity test passes; full suite has no new failures introduced by
`src/panes/__init__.py`/`registry.py` (these are new, additive files with
no imports into existing modules, so no regression is expected — this step
confirms it).

- [ ] **Step 5: Manual verification checklist**

Since there is no E2E infra in this repo (per `dashboard/CLAUDE.md` and the
shell spec §9.2's own admission), record these as a manual pass — run `cd
dashboard && npm run dev` against a live daemon (`./run.sh start` from repo
root) and, using React DevTools or a temporary debug route to mount
`<ContextualSettingsPane>` directly with hand-supplied props (there is no
`<ShellPane>` yet to open it through — see Global Constraints), confirm:

- [ ] `project` subject loads a real project's config, edits, saves, and
      the daemon's `GET /api/projects` reflects the change afterward.
- [ ] `profile` subject loads a real system profile, the intelligence
      class picker's options match `Settings → Intelligence Classes`.
- [ ] `project-profile` subject with no scoped override shows the "No
      project override exists yet" banner and a disabled Save.
- [ ] `playbook` subject: editing and saving a real playbook's source
      triggers a recompile; deliberately saving with a stale
      `expected_source_hash` (edit the same playbook in two panes/tabs)
      surfaces the conflict message without losing the draft.
- [ ] `intelligence-class` subject renders one real class's provider
      matrix and no save/discard buttons appear.
- [ ] `$mod-s` saves; `Esc` on a clean form closes without a prompt; `Esc`
      on a dirty form prompts via `window.confirm`, and Cancel leaves the
      draft untouched.

- [ ] **Step 6: Record deviations** (see report below — no commit needed for
      this task; Task 1–18 commits already cover all source changes)

---

## Self-Review Notes

**Spec coverage:** Every `contextual-settings` spec section maps to a task
— §3 manifest → Task 8; §4 args → Task 7; §5 component/subjects → Tasks
10–15; §6 toolbar/shortcuts → Tasks 10–15, 18, and shortcut-ownership in
Task 15; §7 data/queries → Task 6 (new hook) plus each subject task; §8
save/dirty/confirm-discard → Task 9 + Task 15; §9 loading/error/not-found →
covered per-subject in Tasks 10–14's component code and tests; §10
agent-push examples → not implemented (no messaging/`aq message send
--pane-open` wiring exists yet — that's the plugin-interface spec's own
scope, not a per-view task, and is unaffected by this plan); §11 tests →
Tasks 7, 8, 10–16, 18; §12 implementation checklist → every bullet has a
corresponding task (Tasks 3–18); §13 open questions → intentionally left
open, as the spec itself designates them.

**Palette wiring (spec §6.3):** No command palette exists in this repo
(part of the shell spec's Phase B, not built here). This plan does not
attempt to wire `resolveContextualSettingsPaletteArgs`-style focus
resolution into a nonexistent palette; `manifest.palette_label`/
`palette_section` are set (Task 8) so a future palette integration can
consume them with zero changes to this view, matching the plugin-interface
spec's "a view never needs to touch shell code."

**Placeholder scan:** No task contains "TBD"/"add error handling"/"similar
to Task N" without inline code — every step's code block is complete and
runnable against the exact hook signatures and payload shapes confirmed
from the current source files.

**Type consistency:** `FormState` (project, from `Config.tsx`) and
`ProfileFormState` (profile/project-profile, from `profileForm.ts`) are
deliberately two distinct names to avoid a collision when both are
imported into the same `index.tsx`/test-utils barrel in future work — every
task that imports one aliases it locally as `FormState` for readability
within that file only, never re-exporting the alias.
