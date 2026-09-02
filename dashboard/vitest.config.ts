import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Read via globalThis so this config typechecks without @types/node
// (same convention as vite.config.ts).
const env =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};

// This box runs several agents at once, so the suite must not size itself
// from the raw core count — Vitest's default is half the cores, which on a
// 24-core box shared by six agents is ~72 forks fighting for CPU, and that
// contention is what pushed findBy*/waitFor past their timeouts. The daemon's
// resource gating exports this session's CPU share; outside a session, fall
// back to a modest cap. VITEST_MAX_WORKERS is the manual override.
const share = Number(
  env.VITEST_MAX_WORKERS ??
    env.AQ_TEST_WORKERS ??
    env.AQ_CPU_SHARE ??
    env.PYTEST_XDIST_AUTO_NUM_WORKERS ??
    0,
);
const maxWorkers = Number.isFinite(share) && share > 0 ? share : 4;

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
    css: false,
    pool: "forks",
    // Isolation stays ON. Sharing one module registry and one jsdom across
    // files made the suite order-dependent: leaked `vi.mock` registrations,
    // a poisoned `navigator.clipboard` that stopped whole files from being
    // collected, and a different victim each run, because which files share
    // a worker changes. (Vitest 4 also removed `poolOptions.forks.singleFork`
    // — silently, since this file was not type-checked — so the serialisation
    // it was meant to provide had not been in effect at all.)
    isolate: true,
    maxWorkers,
    // Comfortably above the 5s asyncUtilTimeout in setupTests.ts, so a
    // findBy* that really cannot resolve reports its own "unable to find"
    // error instead of being cut short by the runner's generic timeout.
    testTimeout: 15_000,
  },
});
