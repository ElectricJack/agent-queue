/**
 * Contract coverage for two independently mounted dashboards.  Each browser
 * gets its own QueryClient, DashboardStateProvider, and event subscription;
 * the fake server is the only shared state.  This deliberately exercises the
 * same bootstrap, event invalidation, and reconnect paths as production,
 * without coupling the test to a particular rendered screen.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { DashboardStateProvider } from "../DashboardStateProvider";
import type { DashboardStateDocument } from "../dashboardState";
import { DEFAULT_VALUES, useDashboardDocumentState } from "../dashboardStateStore";
import {
  createFakeDashboardStateServer,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";
import {
  __dispatchEventForTests,
  __setConnectionStatusForTests,
  useEventStream,
} from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";

const ADA = "human:ada";
const GRACE = "human:grace";
const PROJECT_A = "project-a";
const PROJECT_B = "project-b";

function browser(server: FakeDashboardStateServer, owner = ADA) {
  const client = testQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <DashboardStateProvider transport={server.transport(owner)}>{children}</DashboardStateProvider>
    </QueryClientProvider>
  );
  return { client, wrapper };
}

function useBrowserState() {
  // A real dashboard mounts this alongside DashboardStateProvider.  Keeping it
  // in each isolated test context proves the event fan-out path as well.
  useEventStream();
  return {
    navigation: useDashboardDocumentState("nav_organization"),
    shell: useDashboardDocumentState("shell_preferences"),
    commandCenter: useDashboardDocumentState("command_center_preferences"),
    project: useDashboardDocumentState("command_center_project_view", PROJECT_A),
    otherProject: useDashboardDocumentState("command_center_project_view", PROJECT_B),
    playbook: useDashboardDocumentState("playbook_graph_view"),
  };
}

function changed(document: DashboardStateDocument): NotifyEvent {
  return {
    _event_type: "dashboard_state.changed.v1",
    event_type: "dashboard_state.changed.v1",
    version: 1,
    scope: document.scope,
    owner_id: document.owner_id,
    namespace: document.namespace,
    subject: document.subject,
    revision: document.revision,
    change: "write",
    updated_at: document.updated_at,
  } as NotifyEvent;
}

function expectReady(state: ReturnType<typeof useBrowserState>) {
  expect(state.navigation.status).toBe("ready");
  expect(state.shell.status).toBe("ready");
  expect(state.commandCenter.status).toBe("ready");
  expect(state.project.status).toBe("ready");
  expect(state.otherProject.status).toBe("ready");
  expect(state.playbook.status).toBe("ready");
}

function writeSeed(server: FakeDashboardStateServer) {
  server.write("nav_organization", {
    folders: [{ id: "shared", name: "Shared work", collapsed: true }],
    assignments: { [PROJECT_A]: "shared" },
    project_order: [PROJECT_A],
  });
  server.write("shell_preferences", {
    ...DEFAULT_VALUES.shell_preferences,
    theme: "light",
    pane_widths: { "task-detail": 640 },
    agent_flock_collapsed: true,
    last_project_id: PROJECT_A,
  }, { owner: ADA });
  server.write("command_center_preferences", { density: "spacious" }, { owner: ADA });
  server.write("command_center_project_view", {
    expanded_task_ids: ["task-a"],
    expanded_finished_task_ids: [],
    manual_positions: { "task-a": { x: 10, y: 20 } },
  }, { owner: ADA, subject: PROJECT_A });
  server.write("playbook_graph_view", {
    manual_positions: { start: { x: 1, y: 2 } },
  }, { owner: ADA });
}

afterEach(() => {
  __setConnectionStatusForTests("disconnected");
  window.localStorage.clear();
});

describe("dashboard-state server contract across isolated browsers", () => {
  it("loads, live-syncs, reloads, reconnects, isolates, and resets every namespace", async () => {
    const server = createFakeDashboardStateServer();
    writeSeed(server);

    const laptop = renderHook(useBrowserState, { wrapper: browser(server).wrapper });
    const desktop = renderHook(useBrowserState, { wrapper: browser(server).wrapper });
    await waitFor(() => expectReady(laptop.result.current));
    await waitFor(() => expectReady(desktop.result.current));

    // Load-time values are the shared backend values. That no retired browser
    // key is read is enforced by tests/test_dashboard_browser_storage.py.
    expect(desktop.result.current.navigation.value.folders?.[0]).toMatchObject({
      id: "shared", name: "Shared work", collapsed: true,
    });
    expect(desktop.result.current.shell.value).toMatchObject({
      theme: "light", pane_widths: { "task-detail": 640 }, agent_flock_collapsed: true,
    });
    expect(desktop.result.current.commandCenter.value).toEqual({ density: "spacious" });
    expect(desktop.result.current.project.value).toMatchObject({
      expanded_task_ids: ["task-a"], manual_positions: { "task-a": { x: 10, y: 20 } },
    });
    expect(desktop.result.current.playbook.value).toEqual({
      manual_positions: { start: { x: 1, y: 2 } },
    });
    expect(desktop.result.current.otherProject.value).toEqual(DEFAULT_VALUES.command_center_project_view);

    // A write accepted by the shared backend reaches both independent browser
    // caches through the same event frame and address-specific refetch.
    const updates: Array<[keyof ReturnType<typeof useBrowserState>, DashboardStateDocument]> = [
      ["navigation", server.write("nav_organization", {
        folders: [{ id: "shared", name: "Renamed shared work", collapsed: false }],
        assignments: { [PROJECT_A]: "shared" }, project_order: [PROJECT_A, PROJECT_B],
      })],
      ["shell", server.write("shell_preferences", {
        ...DEFAULT_VALUES.shell_preferences, theme: "system", projects_section_open: false,
      }, { owner: ADA })],
      ["commandCenter", server.write("command_center_preferences", { density: "compact" }, { owner: ADA })],
      ["project", server.write("command_center_project_view", {
        expanded_task_ids: ["task-a", "task-b"], expanded_finished_task_ids: ["task-a"],
        manual_positions: { "task-b": { x: 30, y: 40 } },
      }, { owner: ADA, subject: PROJECT_A })],
      ["playbook", server.write("playbook_graph_view", {
        manual_positions: { finish: { x: 3, y: 4 } },
      }, { owner: ADA })],
    ];
    await act(async () => {
      for (const [, document] of updates) __dispatchEventForTests(changed(document));
    });
    await waitFor(() => {
      for (const [key, document] of updates) {
        expect(laptop.result.current[key].revision).toBe(document.revision);
        expect(desktop.result.current[key].revision).toBe(document.revision);
      }
    });

    // A fresh QueryClient is a reload, not a copy of either existing cache.
    const reload = renderHook(useBrowserState, { wrapper: browser(server).wrapper });
    await waitFor(() => expectReady(reload.result.current));
    expect(reload.result.current.navigation.value.folders?.[0]?.name).toBe("Renamed shared work");
    expect(reload.result.current.shell.value.theme).toBe("system");
    expect(reload.result.current.commandCenter.value.density).toBe("compact");
    expect(reload.result.current.project.value.expanded_task_ids).toEqual(["task-a", "task-b"]);
    expect(reload.result.current.playbook.value.manual_positions).toEqual({ finish: { x: 3, y: 4 } });

    // A missed event is recovered by the authoritative bootstrap on reconnect.
    const afterMissedEvent = server.write("shell_preferences", {
      ...DEFAULT_VALUES.shell_preferences, theme: "dark", last_project_id: PROJECT_B,
    }, { owner: ADA });
    await act(async () => {
      __setConnectionStatusForTests("connected");
    });
    await waitFor(() => expect(desktop.result.current.shell.revision).toBe(afterMissedEvent.revision));
    expect(laptop.result.current.shell.value).toMatchObject({ theme: "dark", last_project_id: PROJECT_B });

    // User documents do not cross principals, while workspace navigation does.
    const colleague = renderHook(useBrowserState, { wrapper: browser(server, GRACE).wrapper });
    await waitFor(() => expectReady(colleague.result.current));
    expect(colleague.result.current.navigation.value.folders?.[0]?.name).toBe("Renamed shared work");
    expect(colleague.result.current.shell.value).toEqual(DEFAULT_VALUES.shell_preferences);
    expect(colleague.result.current.commandCenter.value).toEqual(DEFAULT_VALUES.command_center_preferences);
    expect(colleague.result.current.project.value).toEqual(DEFAULT_VALUES.command_center_project_view);
    expect(colleague.result.current.playbook.value).toEqual(DEFAULT_VALUES.playbook_graph_view);

    // Reset remains an authoritative revision: every same-user context returns
    // to server defaults, and the project-addressed row leaves PROJECT_B alone.
    await act(async () => {
      await Promise.all([
        desktop.result.current.navigation.reset(),
        desktop.result.current.shell.reset(),
        desktop.result.current.commandCenter.reset(),
        desktop.result.current.project.reset(),
        desktop.result.current.playbook.reset(),
      ]);
    });
    const resetDocuments = [
      server.document("nav_organization"),
      server.document("shell_preferences", { owner: ADA }),
      server.document("command_center_preferences", { owner: ADA }),
      server.document("command_center_project_view", { owner: ADA, subject: PROJECT_A }),
      server.document("playbook_graph_view", { owner: ADA }),
    ];
    await act(async () => {
      for (const document of resetDocuments) __dispatchEventForTests(changed(document));
    });
    await waitFor(() => {
      expect(laptop.result.current.navigation.value).toEqual(DEFAULT_VALUES.nav_organization);
      expect(laptop.result.current.shell.value).toEqual(DEFAULT_VALUES.shell_preferences);
      expect(laptop.result.current.commandCenter.value).toEqual(DEFAULT_VALUES.command_center_preferences);
      expect(laptop.result.current.project.value).toEqual(DEFAULT_VALUES.command_center_project_view);
      expect(laptop.result.current.playbook.value).toEqual(DEFAULT_VALUES.playbook_graph_view);
    });
    expect(server.document("command_center_project_view", { owner: ADA, subject: PROJECT_B })).toMatchObject({
      revision: 0, exists: false,
    });

    // The transport derives the user; callers cannot choose an owner in a
    // request body, which prevents browser-supplied cross-user writes.
    for (const call of server.calls) {
      expect(JSON.stringify(call.body ?? {})).not.toMatch(/owner|user_id|human_id|principal/);
    }
  });
});
