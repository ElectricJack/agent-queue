import { cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterAll, afterEach, describe, expect, it, vi } from "vitest";

const transport = vi.hoisted(() => {
  const api = {
    list: vi.fn(),
    get: vi.fn(),
  };
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    static instance: Socket;
    readyState = 0;
    onopen?: () => void;
    onmessage?: (event: { data: string }) => void;
    onclose?: () => void;
    constructor() { Socket.instance = this; }
    close() {}
  }
  vi.stubGlobal("WebSocket", Socket);
  return { api, Socket };
});

vi.mock("../client", () => ({
  dashboardStateList: transport.api.list,
  dashboardStateGet: transport.api.get,
}));

import { DashboardStateProvider } from "../DashboardStateProvider";
import { useDashboardStateStatus } from "../dashboardStateContext";
import {
  DASHBOARD_STATE_BOOTSTRAP_KEY,
  dashboardStateDocumentKey,
  seedDashboardStateDocuments,
  type DashboardStateDocument,
} from "../dashboardState";
import { useEventStream } from "../../ws/useEventStream";

function navDocument(revision: number, name: string): DashboardStateDocument {
  return {
    scope: "workspace",
    owner_id: "",
    namespace: "nav_organization",
    subject: null,
    revision,
    exists: true,
    value: { folders: [{ id: "folder", name }] },
    updated_at: revision,
  };
}

function LiveConsumer() {
  useEventStream();
  const { status, ownerId } = useDashboardStateStatus();
  return <span>{status}:{ownerId}</span>;
}

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <DashboardStateProvider>{children}</DashboardStateProvider>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  transport.api.list.mockReset();
  transport.api.get.mockReset();
});
afterAll(() => vi.unstubAllGlobals());

describe("DashboardStateProvider", () => {
  it("refetches and adopts the authoritative bootstrap on every connection", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let authoritative = navDocument(1, "Before");
    transport.api.list.mockImplementation(async () => ({
      data: { success: true, owner_id: "human:local-operator", documents: [authoritative] },
    }));

    render(<LiveConsumer />, { wrapper: wrapper(client) });
    await waitFor(() => {
      expect(client.getQueryData(dashboardStateDocumentKey("nav_organization", null)))
        .toEqual(authoritative);
    });

    // No event is delivered for revision 3: reconnect is the gap-recovery path.
    authoritative = navDocument(3, "After a missed event");
    transport.Socket.instance.onopen?.();
    await waitFor(() => {
      expect(client.getQueryData(DASHBOARD_STATE_BOOTSTRAP_KEY))
        .toMatchObject({ documents: [{ revision: 3 }] });
      expect(client.getQueryData(dashboardStateDocumentKey("nav_organization", null)))
        .toEqual(authoritative);
    });
    expect(transport.api.list).toHaveBeenCalledTimes(2);
    client.clear();
  });

  it("never lets a slower bootstrap replace a newer document revision", () => {
    const client = new QueryClient();
    const newest = navDocument(5, "Newest");
    client.setQueryData(dashboardStateDocumentKey("nav_organization", null), newest);

    seedDashboardStateDocuments(client, {
      success: true,
      owner_id: "human:local-operator",
      documents: [navDocument(4, "Stale bootstrap")],
    });

    expect(client.getQueryData(dashboardStateDocumentKey("nav_organization", null)))
      .toEqual(newest);
    client.clear();
  });
});
