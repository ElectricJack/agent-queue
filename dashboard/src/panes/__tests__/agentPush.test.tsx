import { renderHook, act, waitFor } from "@testing-library/react";
import { beforeEach } from "vitest";
import { z } from "zod";
import type { ReactNode } from "react";
import { ShellPaneProvider, useShellPaneStore } from "../store";
import { useAgentPushBridge } from "../agentPush";
import type { PaneEntry } from "../registry";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
} from "../../testUtils/dashboardState";
import { useShellPreferences } from "../../shell/useShellPreferences";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let fakeEventCbs: ((e: any) => void)[] = [];
vi.mock("../../ws/useEventStream", () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useEventStream: ({ onEvent }: { onEvent: (e: any) => void }) => {
    fakeEventCbs.push(onEvent);
  },
}));

const registry: Record<string, PaneEntry> = {
  "mock-view": {
    manifest: {
      id: "mock-view",
      name: "Mock",
      description: "test",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      icon: (() => null) as any,
      args_schema: z.object({ taskId: z.string() }),
      agent_pushable: true,
    },
    Component: () => null,
  },
  "locked-view": {
    manifest: {
      id: "locked-view",
      name: "Locked",
      description: "test",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      icon: (() => null) as any,
      args_schema: z.object({ taskId: z.string() }),
      agent_pushable: false,
    },
    Component: () => null,
  },
};

function BridgeHost() {
  useAgentPushBridge();
  return null;
}

function useProbe() {
  return { pane: useShellPaneStore(), preferences: useShellPreferences() };
}

let server = createFakeDashboardStateServer();
beforeEach(() => {
  server = createFakeDashboardStateServer();
  fakeEventCbs = [];
});

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={testQueryClient()}>
    <TestDashboardState server={server}>
      <ShellPaneProvider registryOverride={registry}>
        <BridgeHost />
        {children}
      </ShellPaneProvider>
    </TestDashboardState>
  </QueryClientProvider>
);

test("valid pane_open frame opens the pane", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => {
    fakeEventCbs.forEach((callback) => callback({
      event_type: "message.sent",
      to_kind: "user",
      pane_open: { view: "mock-view", args: { taskId: "t1" } },
    }));
  });
  expect(result.current.state).toMatchObject({
    kind: "open",
    view: "mock-view",
    args: { taskId: "t1" },
  });
});

test("pane_open frame for non-pushable view is ignored", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => {
    fakeEventCbs.forEach((callback) => callback({
      event_type: "message.sent",
      to_kind: "user",
      pane_open: { view: "locked-view", args: { taskId: "t1" } },
    }));
  });
  expect(result.current.state).toEqual({ kind: "closed" });
});

test("pane_open frame not addressed to user is ignored", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => {
    fakeEventCbs.forEach((callback) => callback({
      event_type: "message.sent",
      to_kind: "session",
      pane_open: { view: "mock-view", args: { taskId: "t1" } },
    }));
  });
  expect(result.current.state).toEqual({ kind: "closed" });
});

test("agent pushes open transient panes in every dashboard without right-surface writes", async () => {
  const first = renderHook(useProbe, { wrapper });
  const second = renderHook(useProbe, { wrapper });
  await waitFor(() => expect(first.result.current.preferences.status).toBe("ready"));
  await waitFor(() => expect(second.result.current.preferences.status).toBe("ready"));

  act(() => {
    fakeEventCbs.forEach((callback) => callback({
      event_type: "message.sent",
      to_kind: "user",
      pane_open: { view: "mock-view", args: { taskId: "t1" } },
    }));
  });

  for (const result of [first.result, second.result]) {
    expect(result.current.pane.state).toMatchObject({ kind: "open", view: "mock-view", args: { taskId: "t1" } });
    expect(result.current.pane.origin).toBe("agent");
  }
  expect(server.calls.filter((call) => call.op === "put")).toEqual([]);
});
