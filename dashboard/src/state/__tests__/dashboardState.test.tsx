import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { NavOrganization } from "../../api/client";
import {
  DashboardStateConflictError,
  DashboardStateProvider,
  DEFAULT_VALUES,
  dashboardDocumentKey,
  MAX_CONFLICTS,
  useDashboardDocument,
  useDashboardStateOwner,
  type DashboardStateTransport,
} from "../dashboardState";
import {
  createFakeDashboardStateServer,
  LOCAL_OPERATOR,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";

/** One dashboard (its own page and query cache) connected to `server`. */
function dashboard(
  server: FakeDashboardStateServer,
  { owner = LOCAL_OPERATOR, transport }: { owner?: string; transport?: DashboardStateTransport } = {},
) {
  const client = testQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <DashboardStateProvider transport={transport ?? server.transport(owner)}>
        {children}
      </DashboardStateProvider>
    </QueryClientProvider>
  );
  return { client, wrapper };
}

const puts = (server: FakeDashboardStateServer) => server.calls.filter((call) => call.op === "put");
const baseRevisions = (server: FakeDashboardStateServer) =>
  puts(server).map((call) => (call.body as { base_revision: number | null }).base_revision);

afterEach(() => vi.restoreAllMocks());

describe("useDashboardDocument", () => {
  it("renders the namespace default while loading, then the server's document", async () => {
    const server = createFakeDashboardStateServer();
    server.write("command_center_preferences", { density: "compact" });
    const release = server.hold();
    const { result } = renderHook(() => useDashboardDocument("command_center_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    expect(result.current).toMatchObject({
      status: "loading",
      value: { density: "comfortable" },
      revision: 0,
      exists: false,
    });
    release();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current).toMatchObject({ value: { density: "compact" }, revision: 1, exists: true });
  });

  it("serves a never-written document as the default at revision 0", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current).toMatchObject({
      value: DEFAULT_VALUES.shell_preferences,
      revision: 0,
      exists: false,
    });
  });

  it("reports unavailable, keeps the default and refuses writes while the server cannot answer", async () => {
    const server = createFakeDashboardStateServer();
    server.failWith(new Error("API 503: daemon unavailable"));
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("unavailable"));
    expect(result.current.value).toEqual(DEFAULT_VALUES.shell_preferences);

    let failure: unknown;
    await act(async () => {
      failure = await result.current
        .update((prefs) => ({ ...prefs, theme: "light" }))
        .catch((error: unknown) => error);
    });
    expect(String(failure)).toContain("503");
    expect(result.current.value).toEqual(DEFAULT_VALUES.shell_preferences);
    expect(result.current.error?.message).toContain("503");
    expect(puts(server)).toHaveLength(0);

    // Once the daemon answers again, the next change is accepted normally.
    server.failWith(null);
    await act(() => result.current.update((prefs) => ({ ...prefs, theme: "light" })));
    expect(result.current).toMatchObject({
      status: "ready",
      value: { theme: "light" },
      revision: 1,
      error: null,
    });
  });

  it("shows a change at once and adopts the revision the server gives it", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("command_center_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const release = server.hold();
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.write({ density: "spacious" });
    });
    expect(result.current).toMatchObject({ value: { density: "spacious" }, revision: 0 });
    release();
    await act(() => pending);
    expect(result.current).toMatchObject({ value: { density: "spacious" }, revision: 1, exists: true });
    expect(puts(server)[0]?.body).toEqual({
      namespace: "command_center_preferences",
      subject: null,
      base_revision: 0,
      value: { density: "spacious" },
    });
  });

  it("shows the server's value again when a write fails, and reports why", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("command_center_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    server.failWith(new Error("API 500: refused"));
    let failure: unknown;
    await act(async () => {
      failure = await result.current.write({ density: "compact" }).catch((error: unknown) => error);
    });
    expect(String(failure)).toContain("refused");
    expect(result.current).toMatchObject({ value: { density: "comfortable" }, revision: 0 });
    expect(result.current.error?.message).toContain("refused");
    expect(server.document("command_center_preferences").revision).toBe(0);

    server.failWith(null);
    await act(() => result.current.write({ density: "compact" }));
    expect(result.current).toMatchObject({ value: { density: "compact" }, error: null });
  });

  it("re-applies an operation to the newer document when another dashboard wrote first", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("nav_organization"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    server.write("nav_organization", {
      folders: [{ id: "f-a", name: "A", collapsed: false }],
      assignments: {},
      project_order: [],
    });
    await act(() =>
      result.current.update((org) => ({
        ...org,
        folders: [...(org.folders ?? []), { id: "f-b", name: "B", collapsed: false }],
      })),
    );
    const stored = server.document("nav_organization").value as NavOrganization;
    expect(stored.folders?.map((folder) => folder.id)).toEqual(["f-a", "f-b"]);
    expect(baseRevisions(server)).toEqual([0, 1]);
    expect(result.current.revision).toBe(2);
    expect((result.current.value.folders ?? []).map((folder) => folder.id)).toEqual(["f-a", "f-b"]);
  });

  it(`gives up after ${MAX_CONFLICTS} consecutive conflicts and shows the server's document`, async () => {
    const server = createFakeDashboardStateServer();
    const direct = server.transport();
    let competitor = 0;
    const racing: DashboardStateTransport = {
      ...direct,
      async put(body) {
        competitor += 1;
        server.write("playbook_graph_view", {
          manual_positions: { ["other-" + competitor]: { x: competitor, y: 0 } },
        });
        return direct.put(body);
      },
    };
    const { result } = renderHook(() => useDashboardDocument("playbook_graph_view"), {
      wrapper: dashboard(server, { transport: racing }).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    let failure: unknown;
    await act(async () => {
      failure = await result.current
        .update((view) => ({ manual_positions: { ...view.manual_positions, mine: { x: 1, y: 1 } } }))
        .catch((error: unknown) => error);
    });
    expect(failure).toBeInstanceOf(DashboardStateConflictError);
    expect(puts(server)).toHaveLength(MAX_CONFLICTS);
    expect(result.current.revision).toBe(MAX_CONFLICTS);
    expect(result.current.value).toEqual(server.document("playbook_graph_view").value);
  });

  it("serialises a burst of changes into a chain of revisions instead of conflicts", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => {
      const { update } = result.current;
      await Promise.all([
        update((prefs) => ({ ...prefs, agent_flock_collapsed: true })),
        update((prefs) => ({ ...prefs, projects_section_open: false })),
        update((prefs) => ({ ...prefs, theme: "light" })),
      ]);
    });
    expect(baseRevisions(server)).toEqual([0, 1, 2]);
    expect(server.document("shell_preferences")).toMatchObject({
      revision: 3,
      value: { agent_flock_collapsed: true, projects_section_open: false, theme: "light" },
    });
  });

  it("applies a change made before the first load to the stored document, not to the defaults", async () => {
    const server = createFakeDashboardStateServer();
    server.write("shell_preferences", {
      ...DEFAULT_VALUES.shell_preferences,
      theme: "light",
      pane_widths: { "task-detail": 640 },
    });
    const release = server.hold();
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.update((prefs) => ({ ...prefs, agent_flock_collapsed: true }));
    });
    expect(result.current.status).toBe("loading");
    release();
    await act(() => pending);
    expect(server.document("shell_preferences").value).toMatchObject({
      theme: "light",
      pane_widths: { "task-detail": 640 },
      agent_flock_collapsed: true,
    });
    expect(baseRevisions(server)).toEqual([1]);
  });

  it("skips the write when a change leaves the value as it is", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(() => result.current.update((prefs) => ({ ...prefs })));
    expect(puts(server)).toHaveLength(0);
  });

  it("resets to the default at a higher revision", async () => {
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("command_center_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(() => result.current.write({ density: "compact" }));
    await act(() => result.current.reset());
    expect(result.current).toMatchObject({
      value: { density: "comfortable" },
      revision: 2,
      exists: false,
    });
    expect(server.document("command_center_preferences")).toMatchObject({ revision: 2, exists: false });
  });

  it("never replaces a held document with an older one", async () => {
    const server = createFakeDashboardStateServer();
    server.write("command_center_preferences", { density: "compact" });
    const stale = server.document("command_center_preferences");
    server.write("command_center_preferences", { density: "spacious" });
    const direct = server.transport();
    let served = 0;
    const transport: DashboardStateTransport = {
      ...direct,
      async get() {
        served += 1;
        return stale;
      },
    };
    const { client, wrapper } = dashboard(server, { transport });
    const { result } = renderHook(() => useDashboardDocument("command_center_preferences"), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(() =>
      client.invalidateQueries({ queryKey: dashboardDocumentKey("command_center_preferences") }),
    );
    expect(served).toBe(1);
    expect(result.current).toMatchObject({ revision: 2, value: { density: "spacious" } });
  });

  it("keeps project-keyed documents separate per project", async () => {
    const server = createFakeDashboardStateServer();
    const { wrapper } = dashboard(server);
    const { result } = renderHook(
      () => ({
        p1: useDashboardDocument("command_center_project_view", "p1"),
        p2: useDashboardDocument("command_center_project_view", "p2"),
      }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.p1.status).toBe("ready"));
    await act(() => result.current.p1.update((view) => ({ ...view, expanded_task_ids: ["t1"] })));
    expect(server.document("command_center_project_view", { subject: "p1" }).value).toMatchObject({
      expanded_task_ids: ["t1"],
    });
    expect(server.document("command_center_project_view", { subject: "p2" }).revision).toBe(0);
    expect(result.current.p2.value).toEqual(DEFAULT_VALUES.command_center_project_view);
  });

  it("never reads or writes browser storage", async () => {
    const spies = [
      vi.spyOn(Storage.prototype, "getItem"),
      vi.spyOn(Storage.prototype, "setItem"),
      vi.spyOn(Storage.prototype, "removeItem"),
    ];
    const server = createFakeDashboardStateServer();
    const { result } = renderHook(() => useDashboardDocument("shell_preferences"), {
      wrapper: dashboard(server).wrapper,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(() => result.current.update((prefs) => ({ ...prefs, agent_flock_collapsed: true })));
    await act(() => result.current.reset());
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });
});

describe("ownership", () => {
  it("follows the same user to another dashboard and never crosses to another user", async () => {
    const server = createFakeDashboardStateServer();
    const probe = () => ({
      prefs: useDashboardDocument("shell_preferences"),
      organization: useDashboardDocument("nav_organization"),
      owner: useDashboardStateOwner(),
    });

    const laptop = renderHook(probe, { wrapper: dashboard(server, { owner: "human:ada" }).wrapper });
    await waitFor(() => expect(laptop.result.current.prefs.status).toBe("ready"));
    await act(() =>
      laptop.result.current.prefs.update((prefs) => ({
        ...prefs,
        theme: "light",
        agent_flock_collapsed: true,
      })),
    );
    await act(() =>
      laptop.result.current.organization.update((org) => ({
        ...org,
        folders: [{ id: "f-shared", name: "Shared", collapsed: false }],
      })),
    );

    const desktop = renderHook(probe, { wrapper: dashboard(server, { owner: "human:ada" }).wrapper });
    await waitFor(() => expect(desktop.result.current.prefs.status).toBe("ready"));
    expect(desktop.result.current.owner).toBe("human:ada");
    expect(desktop.result.current.prefs).toMatchObject({
      revision: 1,
      value: { theme: "light", agent_flock_collapsed: true },
    });

    const colleague = renderHook(probe, { wrapper: dashboard(server, { owner: "human:grace" }).wrapper });
    await waitFor(() => expect(colleague.result.current.prefs.status).toBe("ready"));
    expect(colleague.result.current.owner).toBe("human:grace");
    expect(colleague.result.current.prefs).toMatchObject({
      revision: 0,
      exists: false,
      value: DEFAULT_VALUES.shell_preferences,
    });
    // Workspace organization is shared by every operator.
    expect(colleague.result.current.organization.value.folders?.map((f) => f.id)).toEqual(["f-shared"]);

    // The owner is derived by the server; no request ever names one.
    for (const call of server.calls) {
      expect(JSON.stringify(call.body ?? {})).not.toMatch(/owner|user_id|human_id|principal/);
    }
  });
});
