import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DASHBOARD_STATE_BOOTSTRAP_KEY,
  dashboardStateDocumentKey,
  type DashboardStateDocument,
} from "../../api/dashboardState";
import { __dispatchEventForTests, useEventStream } from "../useEventStream";
import type { NotifyEvent } from "../types";

vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() {}
  });
});

const ownerId = "human:local-operator";

function navDocument(revision: number, folderName: string): DashboardStateDocument {
  return {
    scope: "workspace",
    owner_id: "",
    namespace: "nav_organization",
    subject: null,
    revision,
    exists: true,
    value: { folders: [{ id: "f1", name: folderName }] },
    updated_at: revision,
  };
}

function shellDocument(revision: number, theme: "dark" | "light"): DashboardStateDocument {
  return {
    scope: "user",
    owner_id: ownerId,
    namespace: "shell_preferences",
    subject: null,
    revision,
    exists: true,
    value: { theme },
    updated_at: revision,
  };
}

function projectDocument(subject: string, revision: number): DashboardStateDocument {
  return {
    scope: "user",
    owner_id: ownerId,
    namespace: "command_center_project_view",
    subject,
    revision,
    exists: true,
    value: { expanded_task_ids: [`${subject}-task`] },
    updated_at: revision,
  };
}

function event(document: DashboardStateDocument, revision = document.revision): NotifyEvent {
  return {
    _event_type: "dashboard_state.changed.v1",
    event_type: "dashboard_state.changed.v1",
    version: 1,
    scope: document.scope,
    owner_id: document.owner_id,
    namespace: document.namespace,
    subject: document.subject,
    revision,
    change: "write",
    updated_at: revision,
  } as NotifyEvent;
}

function makeClient(documents: DashboardStateDocument[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(DASHBOARD_STATE_BOOTSTRAP_KEY, {
    success: true,
    owner_id: ownerId,
    documents,
  });
  return client;
}

function watchDocument(
  client: QueryClient,
  initial: DashboardStateDocument,
  load: () => Promise<DashboardStateDocument>,
) {
  const queryKey = dashboardStateDocumentKey(initial.namespace, initial.subject);
  client.setQueryData(queryKey, initial);
  return renderHook(() => {
    useEventStream();
    return useQuery({ queryKey, queryFn: load });
  }, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

async function flushCoalescedInvalidations(): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(101);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});
afterAll(() => vi.unstubAllGlobals());

describe("dashboard-state WebSocket convergence", () => {
  it("converges two clients after shared and owning-user mutations", async () => {
    let authoritative = navDocument(1, "Before");
    const clientA = makeClient([authoritative]);
    const clientB = makeClient([authoritative]);
    const loadA = vi.fn(async () => authoritative);
    const loadB = vi.fn(async () => authoritative);
    watchDocument(clientA, authoritative, loadA);
    watchDocument(clientB, authoritative, loadB);

    authoritative = navDocument(3, "After");
    clientA.setQueryData(
      dashboardStateDocumentKey("nav_organization", null),
      authoritative,
    );
    act(() => {
      __dispatchEventForTests(event(authoritative, 3));
      __dispatchEventForTests(event(authoritative, 2));
      __dispatchEventForTests(event(authoritative, 3));
    });
    await flushCoalescedInvalidations();

    expect(loadA).not.toHaveBeenCalled();
    expect(loadB).toHaveBeenCalledTimes(1);
    expect(clientA.getQueryData(dashboardStateDocumentKey("nav_organization", null)))
      .toEqual(authoritative);
    expect(clientB.getQueryData(dashboardStateDocumentKey("nav_organization", null)))
      .toEqual(authoritative);

    const oldPreference = shellDocument(0, "dark");
    const newPreference = shellDocument(1, "light");
    clientA.setQueryData(DASHBOARD_STATE_BOOTSTRAP_KEY, {
      success: true, owner_id: ownerId, documents: [authoritative, oldPreference],
    });
    clientB.setQueryData(DASHBOARD_STATE_BOOTSTRAP_KEY, {
      success: true, owner_id: ownerId, documents: [authoritative, oldPreference],
    });
    const preferenceA = vi.fn(async () => newPreference);
    const preferenceB = vi.fn(async () => newPreference);
    watchDocument(clientA, oldPreference, preferenceA);
    watchDocument(clientB, oldPreference, preferenceB);
    clientA.setQueryData(
      dashboardStateDocumentKey("shell_preferences", null),
      newPreference,
    );
    act(() => __dispatchEventForTests(event(newPreference)));
    await flushCoalescedInvalidations();

    expect(preferenceA).not.toHaveBeenCalled();
    expect(preferenceB).toHaveBeenCalledTimes(1);
    expect(clientB.getQueryData(dashboardStateDocumentKey("shell_preferences", null)))
      .toEqual(newPreference);

    clientA.clear();
    clientB.clear();
  });

  it("normalizes replay payloads and scopes invalidation by owner and project", async () => {
    let projectOne = projectDocument("p1", 1);
    const projectTwo = projectDocument("p2", 1);
    const client = makeClient([projectOne, projectTwo]);
    const loadOne = vi.fn(async () => projectOne);
    const loadTwo = vi.fn(async () => projectTwo);
    watchDocument(client, projectOne, loadOne);
    watchDocument(client, projectTwo, loadTwo);

    projectOne = projectDocument("p1", 2);
    act(() => __dispatchEventForTests({
      _event_type: "dashboard_state.changed.v1",
      event_type: "dashboard_state.changed.v1",
      payload: JSON.stringify({
        version: 1,
        scope: "user",
        owner_id: ownerId,
        namespace: "command_center_project_view",
        subject: "p1",
        revision: 2,
        change: "write",
        updated_at: 2,
      }),
    } as unknown as NotifyEvent));
    await flushCoalescedInvalidations();

    expect(loadOne).toHaveBeenCalledTimes(1);
    expect(loadTwo).not.toHaveBeenCalled();
    expect(client.getQueryData(dashboardStateDocumentKey("command_center_project_view", "p1")))
      .toEqual(projectOne);

    act(() => __dispatchEventForTests({
      ...event(projectDocument("p1", 3)),
      owner_id: "human:somebody-else",
    } as NotifyEvent));
    await flushCoalescedInvalidations();
    expect(loadOne).toHaveBeenCalledTimes(1);
    expect(loadTwo).not.toHaveBeenCalled();
    client.clear();
  });
});
