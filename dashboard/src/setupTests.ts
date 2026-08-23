import "@testing-library/jest-dom/vitest";

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
