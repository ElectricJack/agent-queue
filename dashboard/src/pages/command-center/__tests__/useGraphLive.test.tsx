import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ProjectGraphResponse } from "@aq/ts-client";
import { useGraphLive } from "../useGraphLive";
import { projectGraphKey, useProjectGraphs } from "../../../api/graph";
import { registerLayoutRefetch } from "../layout-v2/liveRegistry";
import { layoutExtentKey } from "../../../api/graphLayout";

const transport = vi.hoisted(() => {
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    static instances: Socket[] = [];
    readyState = 0;
    onopen?: () => void;
    onclose?: () => void;
    onmessage?: (message: { data: string }) => void;
    constructor() { Socket.instances.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    close() { this.readyState = 3; this.onclose?.(); }
    receive(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  }
  vi.stubGlobal("WebSocket", Socket);
  return { Socket, get: vi.fn() };
});
vi.mock("@aq/ts-client", async () => ({
  ...await vi.importActual<typeof import("@aq/ts-client")>("@aq/ts-client"),
  getProjectGraphApiProjectsProjectIdGraphGet: (...args: unknown[]) => transport.get(...args),
}));
const clients: QueryClient[] = [];
const snapshots = new Map<string, ProjectGraphResponse>();
const empty = (): ProjectGraphResponse => ({ tasks: [], edges: [], gates: [], agents: [] });
const node = (id: string) => ({ id, title: id, status: "READY", priority: 100 });
const socket = () => transport.Socket.instances[transport.Socket.instances.length - 1]!;

function setup(ids = ["p1"]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return { client, ...renderHook(({ projectIds }) => {
    useGraphLive(projectIds);
    return useProjectGraphs(projectIds);
  }, { initialProps: { projectIds: ids }, wrapper: ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  ) }) };
}

beforeEach(() => {
  snapshots.clear();
  snapshots.set("p1", { ...empty(), tasks: [node("parent"), node("child")] });
  snapshots.set("p2", empty());
  transport.get.mockReset().mockImplementation(({ path }: { path: { project_id: string } }) =>
    Promise.resolve({ data: snapshots.get(path.project_id) ?? empty() }));
  socket().open();
});
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.useRealTimers(); });
afterAll(() => vi.unstubAllGlobals());

describe("shared task workspace live snapshots", () => {
  it.each([
    ["task.created", { ...empty(), tasks: [node("parent"), node("child"), node("new-child")], edges: [{ from: "new-child", to: "parent", dep_type: "parent-child" }] }],
    ["task.reparented", { ...empty(), tasks: [node("parent"), node("child")], edges: [{ from: "child", to: "parent", dep_type: "parent-child" }] }],
    ["task.updated", { ...empty(), tasks: [node("parent"), node("child")], edges: [{ from: "child", to: "parent", dep_type: "blocks" }] }],
    ["task.deleted", { ...empty(), tasks: [node("parent")] }],
    ["task.archived", { ...empty(), tasks: [node("parent")] }],
  ])("refreshes actual task and edge data on raw %s frames", async (eventType, next) => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    snapshots.set("p1", next);
    act(() => socket().receive({ _event_type: eventType, project_id: "p1", task_id: "child" }));
    await waitFor(() => {
      expect(result.current.data.tasks).toEqual(next.tasks);
      expect(result.current.data.edges).toEqual(next.edges);
    }, { timeout: 2000 });
  });

  it("refreshes agents across selected projects when a global agent moves", async () => {
    const { result } = setup(["p1", "p2"]);
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    snapshots.set("p2", { ...empty(), tasks: [node("assigned")], agents: [{ id: "worker", name: "Worker", current_task_id: "assigned" }] });
    act(() => socket().receive({ _event_type: "agent.updated", agent_id: "worker" }));
    await waitFor(() => expect(result.current.data.agents.map((a) => a.id)).toEqual(["worker"]), { timeout: 2000 });
  });

  it("patches progress immediately and then refreshes assignment metadata", async () => {
    const { result, client } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    snapshots.set("p1", { ...empty(), tasks: [{ ...node("child"), status: "IN_PROGRESS", assigned_agent_id: "worker" }], agents: [{ id: "worker", name: "Worker", current_task_id: "child" }] });
    act(() => socket().receive({ _event_type: "notify.task_started", event_type: "notify.task_started", task: { id: "child", project_id: "p1", status: "IN_PROGRESS", assigned_agent: "worker" } }));
    expect(client.getQueryData<ProjectGraphResponse>(projectGraphKey("p1"))?.tasks?.find((t) => t.id === "child")?.status).toBe("IN_PROGRESS");
    await waitFor(() => expect(result.current.data.agents).toHaveLength(1), { timeout: 2000 });
  });

  it("coalesces event bursts without losing the last structural change", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    transport.get.mockClear();
    vi.useFakeTimers();
    act(() => socket().receive({ _event_type: "task.created", project_id: "p1" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    snapshots.set("p1", { ...empty(), tasks: [node("last-created")] });
    act(() => socket().receive({ _event_type: "task.created", project_id: "p1" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.data.tasks.map((t) => t.id)).toEqual(["last-created"]);
    expect(transport.get.mock.calls.length).toBeLessThanOrEqual(2);
  });

  it("replaces an initial request overtaken by an event and ignores its late response", async () => {
    let resolveOld!: (value: { data: ProjectGraphResponse }) => void;
    let oldSignal!: AbortSignal;
    transport.get.mockImplementationOnce(({ signal }: { signal: AbortSignal }) => {
      oldSignal = signal;
      return new Promise<{ data: ProjectGraphResponse }>((resolve) => { resolveOld = resolve; });
    });
    const { result } = setup();
    await waitFor(() => expect(transport.get).toHaveBeenCalledTimes(1));
    snapshots.set("p1", { ...empty(), tasks: [node("created-during-load")] });
    act(() => socket().receive({ _event_type: "task.created", project_id: "p1" }));
    await waitFor(() => expect(result.current.data.tasks.map((task) => task.id)).toEqual(["created-during-load"]), { timeout: 2000 });
    expect(oldSignal.aborted).toBe(true);
    await act(async () => { resolveOld({ data: empty() }); });
    expect(result.current.data.tasks.map((task) => task.id)).toEqual(["created-during-load"]);
  });

  it("lets a slow snapshot finish while a continuous event stream queues another refresh", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    vi.useFakeTimers();
    snapshots.set("p1", { ...empty(), tasks: [node("live-created")] });
    transport.get.mockImplementation(() => new Promise((resolve) => {
      setTimeout(() => resolve({ data: snapshots.get("p1") }), 1200);
    }));
    for (let i = 0; i < 8; i++) {
      act(() => socket().receive({ _event_type: "task.updated", project_id: "p1" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    }
    expect(result.current.data.tasks.map((task) => task.id)).toEqual(["live-created"]);
  });

  it("cancels pending refreshes when the workspace unmounts", async () => {
    const { result, unmount } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    transport.get.mockClear();
    vi.useFakeTimers();
    act(() => socket().receive({ _event_type: "task.created", project_id: "p1" }));
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.get).not.toHaveBeenCalled();
  });

  it("refreshes missed changes after reconnect even without replay frames", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    vi.useFakeTimers();
    act(() => socket().close());
    snapshots.set("p1", { ...empty(), tasks: [node("offline-created")] });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    act(() => socket().open());
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.data.tasks.map((t) => t.id)).toEqual(["offline-created"]);
  });

  it("uses the current project selection and ignores unrelated or unknown events", async () => {
    const { result, rerender } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    rerender({ projectIds: ["p2"] });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    transport.get.mockClear();
    vi.useFakeTimers();
    act(() => {
      socket().receive({ _event_type: "task.created", project_id: "p1" });
      socket().receive({ _event_type: "message.sent", project_id: "p2" });
      socket().receive({ type: "hello", epoch: "test" });
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.get).not.toHaveBeenCalled();
    snapshots.set("p2", { ...empty(), tasks: [node("selected")] });
    act(() => socket().receive({ _event_type: "task.created", project_id: "p2" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.data.tasks.map((t) => t.id)).toEqual(["selected"]);
  });

  it("asks mounted layout layers to refetch after the coalescing window and on reconnect", async () => {
    const refetch = vi.fn();
    const other = vi.fn();
    const unregister = registerLayoutRefetch("p1", refetch);
    const unregisterOther = registerLayoutRefetch("p2", other);
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    vi.useFakeTimers();
    act(() => socket().receive({ _event_type: "task.updated", project_id: "p1", task_id: "child" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(other).not.toHaveBeenCalled();

    act(() => socket().close());
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    act(() => socket().open());
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(refetch).toHaveBeenCalledTimes(2);
    unregister();
    unregisterOther();
  });

  it("invalidates both extent variants so the task count is not a minute stale", async () => {
    const { client } = setup();
    client.setQueryData(layoutExtentKey("p1", "active"), { layout_version: 1, node_count: 13 });
    client.setQueryData(layoutExtentKey("p1", "all"), { layout_version: 1, node_count: 15 });
    client.setQueryData(layoutExtentKey("p2", "active"), { layout_version: 1, node_count: 2 });
    vi.useFakeTimers();
    act(() => socket().receive({ _event_type: "task.updated", project_id: "p1", task_id: "child" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    const stale = (pid: string, variant: "active" | "all") =>
      client.getQueryState(layoutExtentKey(pid, variant))?.isInvalidated;
    expect(stale("p1", "active")).toBe(true);
    expect(stale("p1", "all")).toBe(true);
    expect(stale("p2", "active")).toBe(false);
  });

  it("leaves the canvas untouched by the once-a-second metrics tick", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    const refetch = vi.fn();
    const dispose = registerLayoutRefetch("p1", refetch);
    transport.get.mockClear();
    vi.useFakeTimers();
    try {
      // A minute of ticks. They belong to the Metrics tab and the flock rail;
      // the graph must not re-fetch, re-layout or re-render for any of them.
      for (let i = 0; i < 60; i++) {
        act(() => socket().receive({ _event_type: "metrics.tick", sample: { running_agents: i } }));
      }
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      expect(refetch).not.toHaveBeenCalled();
      expect(transport.get).not.toHaveBeenCalled();
    } finally {
      dispose();
    }
  });

  it("does not refetch the graph for session lifecycle frames", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    expect(transport.get).toHaveBeenCalledTimes(1);
    act(() => socket().receive({ _event_type: "session.started", project_id: "p1", session_id: "s1" }));
    act(() => socket().receive({ _event_type: "session.exited", project_id: "p1", session_id: "s1" }));
    // Past the 500 ms coalescing window: a scheduled refresh would have fired.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 700)); });
    expect(transport.get).toHaveBeenCalledTimes(1);
  });
});
