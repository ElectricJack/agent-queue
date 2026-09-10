import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { DashboardStatePutRequest } from "../api/client";
import { DASHBOARD_STATE_BOOTSTRAP_KEY } from "../api/dashboardState";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../testUtils/dashboardState";
import { __dispatchEventForTests, useEventStream } from "../ws/useEventStream";
import type { NotifyEvent } from "../ws/types";
import {
  createFolder,
  EMPTY_ORGANIZATION,
  moveProject,
  renameFolder,
  toggleFolder,
  type NavOrganization,
} from "./navOrganization";
import { useNavOrganization } from "./useNavOrganization";

// useEventStream opens its socket on import; these tests push frames directly.
vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() {}
  });
});

let server: FakeDashboardStateServer;

beforeEach(() => {
  server = createFakeDashboardStateServer();
});
afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
afterAll(() => vi.unstubAllGlobals());

/** One open dashboard: its own query cache, provider and event subscription. */
function openDashboard(client: QueryClient = testQueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <TestDashboardState server={server}>{children}</TestDashboardState>
    </QueryClientProvider>
  );
  return renderHook(() => {
    useEventStream();
    return useNavOrganization();
  }, { wrapper });
}

type Dashboard = ReturnType<typeof openDashboard>;

async function ready(...dashboards: Dashboard[]) {
  for (const dashboard of dashboards) {
    await waitFor(() => expect(dashboard.result.current.status).toBe("ready"));
  }
}

/** The frame the daemon publishes after a nav_organization write. */
function changed(revision: number): NotifyEvent {
  return {
    _event_type: "dashboard_state.changed.v1",
    event_type: "dashboard_state.changed.v1",
    version: 1,
    scope: "workspace",
    owner_id: "",
    namespace: "nav_organization",
    subject: null,
    revision,
    change: "write",
    updated_at: revision,
  } as NotifyEvent;
}

function puts(): DashboardStatePutRequest[] {
  return server.calls.filter((call) => call.op === "put").map((call) => call.body as DashboardStatePutRequest);
}

function stored(): NavOrganization {
  return server.document("nav_organization").value as NavOrganization;
}

const folderNames = (dashboard: Dashboard) =>
  dashboard.result.current.organization.folders.map((folder) => folder.name);

describe("useNavOrganization", () => {
  it("shows the server default while loading and ignores a retired browser value", async () => {
    const release = server.hold();
    window.localStorage.setItem("aq.shell.project-organization", JSON.stringify({
      folders: [{ id: "legacy", name: "Legacy", collapsed: false }],
      assignments: {},
      order: [],
    }));

    const dashboard = openDashboard();
    expect(dashboard.result.current.status).toBe("loading");
    expect(dashboard.result.current.organization).toEqual(EMPTY_ORGANIZATION);

    act(release);
    await ready(dashboard);
    expect(dashboard.result.current.organization).toEqual(EMPTY_ORGANIZATION);
    expect(puts()).toEqual([]);
  });

  it("persists an operation against the revision it read", async () => {
    server.write("nav_organization", { folders: [], assignments: {}, project_order: ["alpha", "beta"] });
    const dashboard = openDashboard();
    await ready(dashboard);

    await act(() => dashboard.result.current.update((organization) => ({
      ...organization,
      project_order: ["beta", "alpha"],
    })));

    expect(puts()).toEqual([{
      namespace: "nav_organization",
      subject: null,
      base_revision: 1,
      value: { folders: [], assignments: {}, project_order: ["beta", "alpha"] },
    }]);
    expect(stored().project_order).toEqual(["beta", "alpha"]);
    expect(dashboard.result.current.organization.project_order).toEqual(["beta", "alpha"]);
  });

  it("rebases a folder operation on a revision conflict instead of overwriting it", async () => {
    server.write("nav_organization", {
      folders: [{ id: "work", name: "Work", collapsed: false }],
      assignments: {},
      project_order: [],
    });
    const dashboard = openDashboard();
    await ready(dashboard);
    // Another dashboard's write that this one has not heard about yet.
    server.write("nav_organization", {
      folders: [
        { id: "work", name: "Work", collapsed: false },
        { id: "remote", name: "Remote", collapsed: false },
      ],
      assignments: {},
      project_order: [],
    });

    await act(() => dashboard.result.current.update((organization) => toggleFolder(organization, "work")));

    expect(puts().map((put) => put.base_revision)).toEqual([1, 2]);
    const expected = [
      { id: "work", name: "Work", collapsed: true },
      { id: "remote", name: "Remote", collapsed: false },
    ];
    expect(stored().folders).toEqual(expected);
    expect(dashboard.result.current.organization.folders).toEqual(expected);
  });

  it("shows a change made on another open dashboard without a reload", async () => {
    const laptop = openDashboard();
    const desktop = openDashboard();
    await ready(laptop, desktop);

    await act(() => laptop.result.current.update((organization) => createFolder(organization, "Clients").org));
    expect(folderNames(laptop)).toEqual(["Clients"]);
    expect(folderNames(desktop)).toEqual([]);

    act(() => __dispatchEventForTests(changed(1)));
    await waitFor(() => expect(folderNames(desktop)).toEqual(["Clients"]));

    // And back: a rename and an assignment made on the desktop reach the laptop.
    const folderId = desktop.result.current.organization.folders[0]!.id;
    await act(() => desktop.result.current.update((organization) => renameFolder(organization, folderId, "Customers")));
    await act(() => desktop.result.current.update((organization) =>
      moveProject(organization, "p1", { folderId })));
    act(() => {
      __dispatchEventForTests(changed(2));
      __dispatchEventForTests(changed(3));
    });
    await waitFor(() => expect(folderNames(laptop)).toEqual(["Customers"]));
    expect(laptop.result.current.organization.assignments).toEqual({ p1: folderId });
    expect(laptop.result.current.organization.project_order).toEqual(["p1"]);
  });

  it("recovers a change whose event was missed through the reconnect bootstrap", async () => {
    const client = testQueryClient();
    const dashboard = openDashboard(client);
    await ready(dashboard);

    server.write("nav_organization", {
      folders: [{ id: "missed", name: "Missed", collapsed: false }],
      assignments: {},
      project_order: [],
    });
    // What useEventStream does when the socket reconnects.
    await act(() => client.invalidateQueries({ queryKey: DASHBOARD_STATE_BOOTSTRAP_KEY, exact: true }));

    await waitFor(() => expect(folderNames(dashboard)).toEqual(["Missed"]));
  });

  it("refuses writes while the daemon is unavailable", async () => {
    server.failWith(new Error("API 503"));
    const dashboard = openDashboard();
    await waitFor(() => expect(dashboard.result.current.status).toBe("unavailable"));

    await act(() => dashboard.result.current.update((organization) => createFolder(organization, "Lost").org));

    expect(puts()).toEqual([]);
    expect(dashboard.result.current.organization).toEqual(EMPTY_ORGANIZATION);
    expect(dashboard.result.current.error).toBeTruthy();
  });
});
