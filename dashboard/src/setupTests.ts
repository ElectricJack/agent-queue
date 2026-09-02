import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// This box runs several agents concurrently, so a render that takes 50ms
// alone can take a second under load. testing-library's 1000ms default for
// findBy*/waitFor turned that contention into flaky failures; the timeout
// only elapses on a genuine failure, so a wider budget costs passing tests
// nothing.
configure({ asyncUtilTimeout: 5000 });

// Release every test's DOM. RTL auto-cleans when it sees a global afterEach,
// but registering it here keeps that guarantee independent of how the test
// files import the library.
afterEach(cleanup);

// jsdom doesn't implement ResizeObserver. Individual tests that need to
// drive resize callbacks install their own stub (saving/restoring the
// original directly rather than via vi.stubGlobal); this default no-op
// keeps every other test that merely mounts a ResizeObserver-using
// component from crashing.
class NoopResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: typeof NoopResizeObserver }).ResizeObserver =
    NoopResizeObserver;
}
