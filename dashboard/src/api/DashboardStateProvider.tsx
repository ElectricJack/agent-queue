import { useEffect, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  DASHBOARD_STATE_BOOTSTRAP_KEY,
  fetchDashboardStateBootstrap,
  seedDashboardStateDocuments,
} from "./dashboardState";
import { DashboardStateContext, type DashboardStateStatus } from "./dashboardStateContext";

/**
 * Loads the caller's complete dashboard-state snapshot and fans each document
 * into its address-keyed query entry. WebSocket reconnects invalidate the
 * bootstrap key in useEventStream, making this the authoritative gap-recovery
 * path for dropped or missed events.
 */
export function DashboardStateProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const bootstrap = useQuery({
    queryKey: DASHBOARD_STATE_BOOTSTRAP_KEY,
    queryFn: fetchDashboardStateBootstrap,
  });

  useEffect(() => {
    if (bootstrap.data) seedDashboardStateDocuments(queryClient, bootstrap.data);
  }, [bootstrap.data, queryClient]);

  const status: DashboardStateStatus = bootstrap.error
    ? "unavailable"
    : bootstrap.data
      ? "ready"
      : "loading";

  return (
    <DashboardStateContext.Provider
      value={{ ownerId: bootstrap.data?.owner_id ?? null, status }}
    >
      {children}
    </DashboardStateContext.Provider>
  );
}
