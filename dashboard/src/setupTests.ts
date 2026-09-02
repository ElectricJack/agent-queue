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

// jsdom doesn't implement matchMedia. uPlot reads it at import time to pick
// a device pixel ratio, so without this any test that transitively imports a
// chart fails before it runs a single assertion.
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
