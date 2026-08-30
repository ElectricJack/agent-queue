import { useLocation, useNavigate } from "react-router-dom";

export const MAX_AGENT_VIEWS = 4;

/** Ordered, shareable selection. Unknown IDs remain closable in the workspace. */
export function useAgentSelection() {
  const location = useLocation();
  const navigate = useNavigate();
  const selectedIds = location.pathname === "/agents"
    ? [...new Set(new URLSearchParams(location.search).getAll("agent").filter(Boolean))].slice(0, MAX_AGENT_VIEWS)
    : [];

  const navigateTo = (ids: string[], replaceSelection = false) => {
    const search = new URLSearchParams();
    ids.forEach((id) => search.append("agent", id));
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
    close: (id: string) => navigateTo(selectedIds.filter((selected) => selected !== id)),
    resetToken: location.state?.agentSelection === "replace" ? location.key : null,
    locationKey: location.key,
  };
}
