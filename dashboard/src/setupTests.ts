import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// With isolate:false, the library module may be cached across test files.
// Register cleanup from the per-file setup so every test releases its DOM.
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
