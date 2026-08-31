import { afterAll, vi } from "vitest";

vi.hoisted(() => {
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() { this.readyState = 3; }
  }
  vi.stubGlobal("WebSocket", Socket);
});
afterAll(() => vi.unstubAllGlobals());

import { renderHook, act } from "@testing-library/react";
import { z } from "zod";
import type { ReactNode } from "react";
import { ShellPaneProvider, useShellPaneStore } from "../store";
import type { PaneEntry } from "../registry";

const mockRegistry: Record<string, PaneEntry> = {
  "mock-view": {
    manifest: {
      id: "mock-view",
      name: "Mock",
      description: "test",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      icon: (() => null) as any,
      args_schema: z.object({ taskId: z.string() }),
    },
    Component: () => null,
  },
};

const wrapper = ({ children }: { children: ReactNode }) => (
  <ShellPaneProvider registryOverride={mockRegistry}>{children}</ShellPaneProvider>
);

test("initial state is closed", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  expect(result.current.state).toEqual({ kind: "closed" });
});

test("open with valid args transitions to open", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => result.current.open("mock-view", { taskId: "t1" }));
  expect(result.current.state).toMatchObject({
    kind: "open",
    view: "mock-view",
    args: { taskId: "t1" },
  });
});

test("open with invalid args is a no-op (logged)", () => {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  act(() => result.current.open("mock-view", { taskId: 123 as any }));
  expect(result.current.state).toEqual({ kind: "closed" });
  expect(spy).toHaveBeenCalledWith(
    expect.stringContaining("args validation failed for view mock-view"),
    expect.any(Object),
  );
  spy.mockRestore();
});

test("open unknown view id is a no-op (logged)", () => {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => result.current.open("does-not-exist", {}));
  expect(result.current.state).toEqual({ kind: "closed" });
  expect(spy).toHaveBeenCalled();
  spy.mockRestore();
});

test("close returns to closed", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => result.current.open("mock-view", { taskId: "t1" }));
  act(() => result.current.close());
  expect(result.current.state).toEqual({ kind: "closed" });
});

test("setArgs updates open state and re-validates", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => result.current.open("mock-view", { taskId: "t1" }));
  act(() => result.current.setArgs({ taskId: "t2" }));
  expect(result.current.state).toMatchObject({ args: { taskId: "t2" } });
});

test("setWidth persists to localStorage per view id", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => result.current.open("mock-view", { taskId: "t1" }));
  act(() => result.current.setWidth(720));
  expect(localStorage.getItem("aq:shellpane:width:mock-view")).toBe("720");
});
