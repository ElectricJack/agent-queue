import { useCallback, useMemo } from "react";
import { useDashboardDocumentState, type DocumentStatus } from "../api/dashboardStateStore";
import { parseOrganization, type NavOrganization } from "./navOrganization";

export interface NavOrganizationState {
  organization: NavOrganization;
  status: DocumentStatus;
  error: string | null;
  update: (next: (org: NavOrganization) => NavOrganization) => Promise<void>;
}

/**
 * The shared Projects-rail organization (`nav_organization`, one workspace
 * document on the daemon). It reads the address-keyed query entry that the
 * bootstrap seeds, a reconnect re-seeds and `dashboard_state.changed.v1`
 * invalidates, so a folder created, renamed, reordered or assigned on one
 * dashboard appears on every other open one without a reload.
 *
 * Writes go through the shared store's per-address CAS queue: each is an
 * operation over the current value, re-applied to the server's document on a
 * revision conflict instead of overwriting another dashboard.
 */
export function useNavOrganization(): NavOrganizationState {
  const document = useDashboardDocumentState("nav_organization");
  const organization = useMemo(() => parseOrganization(document.value), [document.value]);
  const { update: updateDocument } = document;
  // The rail's handlers fire and forget. A failed write is not rethrown: the
  // store has already put the server's document back on screen, and `error`
  // says why.
  const update = useCallback(
    (next: (org: NavOrganization) => NavOrganization) =>
      updateDocument((value) => next(parseOrganization(value))).catch(() => undefined),
    [updateDocument],
  );
  const error = document.error?.message
    ?? (document.status === "unavailable" ? "Project organization is unavailable" : null);
  return { organization, status: document.status, error, update };
}
