import { renderHook, act } from "@testing-library/react";
import { z } from "zod";
import type { ReactNode } from "react";
import { ShellPaneProvider, useShellPaneStore } from "../store";
import { useAgentPushBridge } from "../agentPush";
import type { PaneEntry } from "../registry";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let fakeEventCb: ((e: any) => void) | null = null;
vi.mock("../../ws/useEventStream", () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useEventStream: ({ onEvent }: { onEvent: (e: any) => void }) => {
    fakeEventCb = onEvent;
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

const wrapper = ({ children }: { children: ReactNode }) => (
  <ShellPaneProvider registryOverride={registry}>
    <BridgeHost />
    {children}
  </ShellPaneProvider>
);

test("valid pane_open frame opens the pane", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => {
    fakeEventCb?.({
      event_type: "message.sent",
      to_kind: "user",
      pane_open: { view: "mock-view", args: { taskId: "t1" } },
    });
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
    fakeEventCb?.({
      event_type: "message.sent",
      to_kind: "user",
      pane_open: { view: "locked-view", args: { taskId: "t1" } },
    });
  });
  expect(result.current.state).toEqual({ kind: "closed" });
});

test("pane_open frame not addressed to user is ignored", () => {
  const { result } = renderHook(() => useShellPaneStore(), { wrapper });
  act(() => {
    fakeEventCb?.({
      event_type: "message.sent",
      to_kind: "session",
      pane_open: { view: "mock-view", args: { taskId: "t1" } },
    });
  });
  expect(result.current.state).toEqual({ kind: "closed" });
});
