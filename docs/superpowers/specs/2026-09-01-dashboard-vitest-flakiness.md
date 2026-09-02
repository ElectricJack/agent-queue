# Dashboard vitest flakiness — root cause and fix

**Task:** `calm-apex` · **Date:** 2026-09-01

## Symptom

Four consecutive `npx vitest run` invocations on a clean tree gave 5, 3, 1 and
1 failures, with the failing *files* changing run to run
(`Workspaces.test.tsx`, `Palette.test.tsx`, `PlaybookGraphCanvas.test.tsx`,
`TaskActions.test.tsx`, `usePlaybookGraph.test.tsx`). Every affected file
passed in isolation. One run discovered only 705 tests instead of 727, so the
test *count* was unstable too.

Reproduced here as a baseline of six consecutive runs: 79 failed / 760,
22 failed / 760, 0 failed but only **738 discovered**, 0 failed / 760,
8 failed / 760, 1 failed / 760.

## Root causes

### 1. `poolOptions.forks.singleFork` was silently dead under Vitest 4

`dashboard/vitest.config.ts` carried:

```ts
pool: "forks",
forks: { singleFork: true },
isolate: false,
```

Commit `519fe2a5` ("pin single-fork pool (v4 top-level config)") moved
`poolOptions.forks` to a top-level `forks` key on the Vitest 4 upgrade. Vitest
4 did remove `poolOptions`, but `singleFork` was **not** relocated — it was
replaced by `maxWorkers` / `fileParallelism`. There is no `forks` key in
Vitest 4's `InlineConfig`.

Evidence:

- `singleFork` appears nowhere in `node_modules/vitest/dist`.
- `node_modules/vitest/dist/chunks/coverage.*.js` warns only about
  `poolOptions`, so an unknown top-level `forks` key produced no diagnostic.
- Wall `Duration 7.6s` against cumulative `tests 49s / environment 110s` —
  the suite was plainly running many workers, not one.

So the suite had been running Vitest's default of *half the box's cores* — ~12
forks on a 24-core machine — while ~6 agents shared that machine. That
contention is what pushed `findBy*`/`waitFor` past testing-library's 1000ms
`asyncUtilTimeout`; every timing failure landed at 1000–1030ms.

The key went unnoticed because `vitest.config.ts` was in no `tsconfig`
`include`, so it was never type-checked.

### 2. `isolate: false` made the suite order-dependent

With N workers and no isolation, files that land in the same worker share the
module registry *and* the jsdom globals — and which files share a worker
changes run to run. Three distinct symptoms, all reproduced:

- **`navigator.clipboard` poisoning.**
  `panes/console-stream/__tests__/index.test.tsx` and
  `panes/session-peek/__tests__/index.test.tsx` did
  `Object.defineProperty(navigator, "clipboard", { value, configurable: true })`
  without `writable: true`, leaving a **non-writable** data property. A later
  file in the same worker doing `Object.assign(navigator, { clipboard: … })`
  then throws `TypeError: Cannot assign to read only property 'clipboard'` in
  strict mode. Where that `Object.assign` sits at module top level —
  `panes/file-browser/__tests__/index.test.tsx:10`,
  `panes/diff-review-changes/__tests__/index.test.tsx:16` — the **whole file
  fails to collect** and reports `(0 test)`.

  *This is the "unstable file discovery", and it is not a discovery bug.*
  Observed directly: a run reporting `Tests 738 passed (738)` with
  `file-browser/__tests__/index.test.tsx (0 test)`.

- **`vi.mock` leakage.** `components/__tests__/TaskActions.test.tsx` mocks
  `react-router-dom`, but received the real module from the shared cache:
  `Error: useLocation() may be used only in the context of a <Router> component`.

- **Stale mock/DOM state.** `components/__tests__/TaskAgentTerminal.test.tsx`:
  `Unable to find an accessible element with the role "button" and name
  "Open agent terminal"`.

### 3. `asyncUtilTimeout` of 1000ms is too tight for a shared box

testing-library's default assumed an idle machine.

## Fix

`dashboard/vitest.config.ts`

- Dropped the dead `forks: { singleFork: true }`.
- `isolate: true`.
- `maxWorkers` from `VITEST_MAX_WORKERS` → `AQ_TEST_WORKERS` → `AQ_CPU_SHARE` →
  `PYTEST_XDIST_AUTO_NUM_WORKERS`, falling back to 4. This is how the suite
  inherits the daemon's per-session CPU share (`src/resources/limits.py`)
  instead of sizing itself from the raw core count.
- `testTimeout: 15_000`, comfortably above the 5s `asyncUtilTimeout`, so a
  `findBy*` that genuinely cannot resolve reports its own "unable to find"
  error rather than being cut short by the runner's generic timeout.
- Env read through `globalThis`, matching `vite.config.ts`'s existing
  convention of not depending on `@types/node`.

`dashboard/src/setupTests.ts`

- `configure({ asyncUtilTimeout: 5000 })`. The timeout only elapses on a
  genuine failure, so a wider budget costs passing tests nothing.

`dashboard/tsconfig.node.json`

- Added `vitest.config.ts` to `include`. Verified this catches the original
  bug: re-adding `forks: { singleFork: true }` now fails typecheck with
  `'forks' does not exist in type 'InlineConfig'`.

`console-stream` / `session-peek` tests

- `writable: true` on the clipboard `defineProperty` calls. Dormant under
  `isolate: true`, but it was the actual latent defect.

## Verification

13 consecutive full runs, every one `88 files / 760 tests / 0 failures`:

| batch | runs | result |
| --- | --- | --- |
| config fix only | 3 | 760 passed |
| all fixes | 4 | 760 passed |
| all fixes, under 20 spinning CPU burners | 2 | 760 passed (~46s) |
| all fixes, final sweep | 5 | 760 passed |

`npm run typecheck` and `npx eslint` on the changed files are clean.

## Cost

Wall time goes from a flaky ~7.6s to a stable ~28s (~46s under heavy load).
The cumulative in-test time actually *drops* (49s → 29s) — the old number was
mostly starvation. Isolation and a worker cap are the price of determinism on
a machine several agents share.

## Not addressed

- The deterministic `pages/command-center/__tests__/hierarchy.test.ts` failure
  named in the task no longer reproduces on this branch; it passes in all 13
  runs above.
- `dashboard/tsconfig.*.tsbuildinfo` are tracked build artifacts, committed
  once at scaffold and never updated. Filed separately.
