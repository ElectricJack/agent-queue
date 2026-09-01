import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

export const MAX_AGENT_VIEWS = 4;

/** Ordered, shareable selection. Unknown IDs remain closable in the workspace. */
export function useAgentSelection() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const selectedIds = location.pathname === "/agents"
    ? [...new Set(params.getAll("agent").filter(Boolean))].slice(0, MAX_AGENT_VIEWS)
    : [];
  const latest = useRef({ selectedIds, mounted: true });
  latest.current.selectedIds = selectedIds;
  useEffect(() => {
    const current = latest.current;
    current.mounted = true;
    return () => { current.mounted = false; };
  }, []);

  const adding = location.pathname === "/agents" && params.get("add") === "1";

  const navigateTo = (ids: string[], replaceSelection = false, add = false) => {
    const search = new URLSearchParams();
    ids.forEach((id) => search.append("agent", id));
    if (add) search.set("add", "1");
    navigate(
      { pathname: "/agents", search: search.toString() },
      { state: replaceSelection ? { agentSelection: "replace" } : null },
    );
  };

  const select = (id: string, additive = false): boolean => {
    if (additive) {
      if (selectedIds.includes(id)) return true;
      if (selectedIds.length >= MAX_AGENT_VIEWS) return false;
      navigateTo([...selectedIds, id]);
    } else {
      navigateTo([id], true);
    }
    return true;
  };

  return {
    selectedIds,
    select,
    /** The Add-agent form is URL state so the left rail can open it from any page. */
    adding,
    setAdding: (next: boolean) => navigateTo(selectedIds, false, next),
    close: (id: string) => {
      // A deletion may finish after selection changes or the workspace unmounts.
      if (!latest.current.mounted || !latest.current.selectedIds.includes(id)) return;
      navigateTo(latest.current.selectedIds.filter((selected) => selected !== id));
    },
    resetToken: location.state?.agentSelection === "replace" ? location.key : null,
    locationKey: location.key,
  };
}
