import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { POOL_PREFIX, poolAddress } from "./pools";

export const MAX_AGENT_VIEWS = 4;

/**
 * A selected view is either one fixed worker or one worker pool. A pool key
 * carries an optional pinned instance after "@" so a tiled layout — and a
 * shared link — survives a reload:
 *
 *     agent=agent-7f1c
 *     agent=pool:agent-queue:worker-standard@p-worker-standard--agent-queue--9f2a
 */
export type AgentSelection =
  | { key: string; kind: "agent"; agentId: string }
  | { key: string; kind: "pool"; projectId: string; profileId: string; instanceId: string | null };

export function poolSelectionKey(projectId: string, profileId: string, instanceId?: string | null) {
  return poolAddress(projectId, profileId) + (instanceId ? "@" + instanceId : "");
}

/** The view a key addresses, with any pinned instance stripped. */
export function selectionAddress(key: string): string {
  const at = key.indexOf("@");
  return at === -1 ? key : key.slice(0, at);
}

export function parseAgentSelection(key: string): AgentSelection {
  if (!key.startsWith(POOL_PREFIX)) return { key, kind: "agent", agentId: key };
  const at = key.indexOf("@");
  const address = at === -1 ? key : key.slice(0, at);
  const instanceId = at === -1 ? null : key.slice(at + 1) || null;
  const rest = address.slice(POOL_PREFIX.length);
  const colon = rest.indexOf(":");
  return {
    key,
    kind: "pool",
    projectId: colon === -1 ? rest : rest.slice(0, colon),
    profileId: colon === -1 ? "" : rest.slice(colon + 1),
    instanceId,
  };
}

/**
 * Which creation surface is open on the agents page.
 *
 * A durable agent and a worker pool are different objects with different
 * scope and lifecycle, so the entry point is a fork rather than one form:
 * ``add=1`` is the fork, ``add=agent`` and ``add=pool`` the two forms.
 * ``add=1`` keeps the value the old boolean flag used, so links that predate
 * the fork still open something sensible.
 */
export type CreateMode = "choice" | "agent" | "pool";

export function parseCreateMode(value: string | null): CreateMode | null {
  if (value === "1" || value === "choice") return "choice";
  if (value === "agent" || value === "pool") return value;
  return null;
}

/** Ordered, shareable selection. Unknown IDs remain closable in the workspace. */
export function useAgentSelection() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  // Dedupe by address, not by key: the same pool pinned to two instances is
  // still one view, and the last pin wins over an earlier bare key.
  const seen = new Set<string>();
  const selectedIds = location.pathname === "/agents"
    ? params.getAll("agent").filter(Boolean).filter((id) => {
      const address = selectionAddress(id);
      if (seen.has(address)) return false;
      seen.add(address);
      return true;
    }).slice(0, MAX_AGENT_VIEWS)
    : [];
  const latest = useRef({ selectedIds, mounted: true });
  latest.current.selectedIds = selectedIds;
  useEffect(() => {
    const current = latest.current;
    current.mounted = true;
    return () => { current.mounted = false; };
  }, []);

  const adding = location.pathname === "/agents" ? parseCreateMode(params.get("add")) : null;

  const navigateTo = (ids: string[], replaceSelection = false, add: CreateMode | null = null) => {
    const search = new URLSearchParams();
    ids.forEach((id) => search.append("agent", id));
    if (add) search.set("add", add === "choice" ? "1" : add);
    navigate(
      { pathname: "/agents", search: search.toString() },
      { state: replaceSelection ? { agentSelection: "replace" } : null },
    );
  };

  const select = (id: string, additive = false): boolean => {
    if (additive) {
      if (selectedIds.some((selected) => selectionAddress(selected) === selectionAddress(id))) return true;
      if (selectedIds.length >= MAX_AGENT_VIEWS) return false;
      navigateTo([...selectedIds, id]);
    } else {
      navigateTo([id], true);
    }
    return true;
  };

  return {
    selectedIds,
    selections: selectedIds.map(parseAgentSelection),
    select,
    /**
     * Re-pin one open pool view to another live instance. The view keeps its
     * tile position, so the surrounding layout does not reflow.
     */
    setInstance: (key: string, instanceId: string | null) => {
      const address = selectionAddress(key);
      const selection = parseAgentSelection(key);
      if (selection.kind !== "pool") return;
      navigateTo(latest.current.selectedIds.map((selected) => selectionAddress(selected) === address
        ? poolSelectionKey(selection.projectId, selection.profileId, instanceId)
        : selected));
    },
    /**
     * The create flow is URL state so the left rail can open it from any page.
     * ``choice`` is the fork an operator lands on first; the two concrete forms
     * are only reachable through it (or a shared link).
     */
    adding,
    setAdding: (next: CreateMode | null) => navigateTo(selectedIds, false, next),
    close: (id: string) => {
      // A deletion may finish after selection changes or the workspace unmounts.
      const address = selectionAddress(id);
      if (!latest.current.mounted) return;
      if (!latest.current.selectedIds.some((selected) => selectionAddress(selected) === address)) return;
      navigateTo(latest.current.selectedIds.filter((selected) => selectionAddress(selected) !== address));
    },
    resetToken: location.state?.agentSelection === "replace" ? location.key : null,
    locationKey: location.key,
  };
}
