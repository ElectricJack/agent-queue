# Pool header terminal selector evidence

Task: sound-impact-72. Multi-instance pools expose the existing native instance
picker in their single-row header and retain the full picker in details. Both
use the same URL selection action. A compact ordinal distinguishes sessions when
their long generated names truncate; the complete context is in each option and
the selected option's tooltip. Single-instance and empty headers are unchanged.

The focused Pools/TerminalPane check passed 68/68 tests. The related agents,
terminal, sessions and focus area check passed 219/219 tests across 15 modules.
Tests cover header switching without details, details/header synchronization,
session additions and removals, fallback to a live session, single-instance
behavior and recovery from an empty pool.

The built-bundle [browser report](layout-report.json) passed 18/18 checks. The
terminal-header check uses native ArrowDown/Enter selection, verifies the pinned
URL and new live terminal, emits a session-exited event and verifies fallback
without opening details. Geometry covers 320/390 px phones, landscape, desktop,
200% zoom, plus the existing small desktop layout. Controls remain in one row,
with 44 px phone targets and no sideways overflow.

Screenshots use synthetic sessions: [320 px selection](phone-320-selection.png)
and [small desktop selection](desktop-selection.png). At the narrowest width the
selector shows its instance number; full names remain available in its options.

Changed-file ESLint and the production build (including TypeScript compilation)
passed. The rebuilt assets were staged in the worker's
`src/dashboard_assets/dist/` with the release integrity manifest (122 files).
Live publication and rebuilding the delivered candidate remain with the
integration owner; this report does not claim a live deployment.

Reproduction from the repository root:

```sh
npm -w dashboard exec -- vitest run src/pages/agents/__tests__/Pools.test.tsx src/components/__tests__/TerminalPane.test.tsx
npm -w dashboard exec -- vitest run src/pages/agents src/components/__tests__/TerminalPane.test.tsx src/components/__tests__/InteractiveTerminal.test.tsx src/components/__tests__/WatchTerminal.test.tsx src/pages/project/__tests__/Sessions.test.tsx src/pages/focus
npm -w dashboard exec -- eslint src/components/TerminalPane.tsx src/pages/agents/PoolWindow.tsx src/pages/agents/__tests__/Pools.test.tsx
npm -w dashboard run build
python scripts/build_release_artifact.py --skip-build
npm -w dashboard run check:layout -- --only terminal-headers,agents-watch-only,focus-session,phone-typing
```
