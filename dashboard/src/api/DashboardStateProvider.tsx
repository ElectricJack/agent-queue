import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DashboardStateContext, type DashboardStateStatus } from "./dashboardStateContext";
import {
  DashboardStateStore,
  DashboardStateStoreContext,
  sdkTransport,
  type DashboardStateTransport,
} from "./dashboardStateStore";

/**
 * Loads the caller's complete dashboard-state snapshot and fans each document
 * into its address-keyed query entry. WebSocket reconnects invalidate the
 * bootstrap key in useEventStream, making this the authoritative gap-recovery
 * path for dropped or missed events. It also owns the write queues handed out
 * by `useDashboardDocumentState` (dashboardStateStore.ts).
 */
export function DashboardStateProvider({
  children,
  transport = sdkTransport,
}: {
  children: ReactNode;
  /** Overridable for tests. */
  transport?: DashboardStateTransport;
}) {
  const queryClient = useQueryClient();
  const [store] = useState(() => new DashboardStateStore(queryClient, transport));
  const bootstrap = useQuery(store.bootstrapOptions);

  const status: DashboardStateStatus = bootstrap.error
    ? "unavailable"
    : bootstrap.data
      ? "ready"
      : "loading";

  return (
    <DashboardStateContext.Provider
      value={{ ownerId: bootstrap.data?.owner_id ?? null, status }}
    >
      <DashboardStateStoreContext.Provider value={store}>{children}</DashboardStateStoreContext.Provider>
    </DashboardStateContext.Provider>
  );
}
