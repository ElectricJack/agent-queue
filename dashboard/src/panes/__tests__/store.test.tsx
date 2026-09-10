import { afterAll, beforeEach, vi } from "vitest";

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

import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { z } from "zod";
import type { ReactNode } from "react";
import { ShellPaneProvider, useShellPaneStore } from "../store";
import type { PaneEntry } from "../registry";
import { SHELL_PREFERENCE_DEFAULTS, useShellPreferences, type ShellPrefs } from "../../shell/useShellPreferences";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";

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

let server: FakeDashboardStateServer;
let client: QueryClient;

beforeEach(() => {
  server = createFakeDashboardStateServer();
  client = testQueryClient();
});

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>
    <TestDashboardState server={server}>
      <ShellPaneProvider registryOverride={mockRegistry}>{children}</ShellPaneProvider>
    </TestDashboardState>
  </QueryClientProvider>
);

function useProbe() {
  return { pane: useShellPaneStore(), preferences: useShellPreferences() };
}

function saved(patch: Partial<ShellPrefs>): ShellPrefs {
  return { ...SHELL_PREFERENCE_DEFAULTS, ...patch };
}

function savedPane(view: string, args: Record<string, unknown>): ShellPrefs {
  return saved({
    right_surface: { ...SHELL_PREFERENCE_DEFAULTS.right_surface, kind: "pane", pane: { view, args } },
  });
}

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

test("setWidth persists the view's width to the user's server preferences", async () => {
  const { result } = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  act(() => result.current.pane.open("mock-view", { taskId: "t1" }));
  act(() => result.current.pane.setWidth(720));
  expect(result.current.pane.state).toMatchObject({ width: 720 });
  await waitFor(() =>
    expect(server.document("shell_preferences").value).toMatchObject({
      pane_widths: { "mock-view": 720 },
    }),
  );
  expect(result.current.pane.state).toMatchObject({ width: 720 });
});

test("a resize gesture is written once, when it settles", async () => {
  const { result } = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  act(() => result.current.pane.open("mock-view", { taskId: "t1" }));
  act(() => {
    result.current.pane.setWidth(600);
    result.current.pane.setWidth(650);
    result.current.pane.setWidth(700);
  });
  expect(result.current.pane.state).toMatchObject({ width: 700 });
  await waitFor(() =>
    expect(server.document("shell_preferences").value).toMatchObject({
      pane_widths: { "mock-view": 700 },
    }),
  );
  const widthWrites = server.calls.filter(
    (call) => call.op === "put" && JSON.stringify(call.body).includes('"pane_widths":{"mock-view"'),
  );
  expect(widthWrites).toHaveLength(1);
});

test("opening a view uses the width saved on the server", async () => {
  server.write("shell_preferences", saved({ pane_widths: { "mock-view": 640 } }));
  const { result } = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  act(() => result.current.pane.open("mock-view", { taskId: "t1" }));
  expect(result.current.pane.state).toMatchObject({ width: 640 });
});

test("open and close record the pane on the server", async () => {
  const { result } = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  act(() => result.current.pane.open("mock-view", { taskId: "t1" }));
  await waitFor(() =>
    expect(server.document("shell_preferences").value).toMatchObject({
      right_surface: { pane: { view: "mock-view", args: { taskId: "t1" } } },
    }),
  );
  act(() => result.current.pane.close());
  await waitFor(() =>
    expect(server.document("shell_preferences").value).toMatchObject({ right_surface: { pane: null } }),
  );
});

test("restores the pane the user last left open once the server answers", async () => {
  server.write("shell_preferences", savedPane("mock-view", { taskId: "t9" }));
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  expect(result.current.state).toEqual({ kind: "closed" });
  await waitFor(() =>
    expect(result.current.state).toMatchObject({ kind: "open", view: "mock-view", args: { taskId: "t9" } }),
  );
});

test.each([
  ["an unregistered view", savedPane("retired-view", { taskId: "t1" })],
  ["args the manifest now rejects", savedPane("mock-view", { taskId: 5 })],
])("a saved pane with %s restores closed", async (_label, value) => {
  server.write("shell_preferences", value);
  const { result } = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  expect(result.current.pane.state).toEqual({ kind: "closed" });
});

test("a pane opened before the server answers is kept, and saved over the old one", async () => {
  server.write("shell_preferences", savedPane("mock-view", { taskId: "old" }));
  const release = server.hold();
  const { result } = renderHook(useProbe, { wrapper });
  act(() => result.current.pane.open("mock-view", { taskId: "new" }));
  release();
  await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
  await waitFor(() =>
    expect(server.document("shell_preferences").value).toMatchObject({
      right_surface: { pane: { args: { taskId: "new" } } },
    }),
  );
  expect(result.current.pane.state).toMatchObject({ args: { taskId: "new" } });
});
