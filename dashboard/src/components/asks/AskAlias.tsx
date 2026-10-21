import { Navigate, useLocation } from "react-router-dom";
import { useAskAlias } from "../../api/asks";
import { focusAskHref } from "../../pages/focus/routes";

/** Resolve the audience-checked alias before loading its document or decisions. */
export function AskAlias({ kind, id }: { kind: "review" | "escalation"; id: string }) {
  const query = useAskAlias(kind, id);
  const location = useLocation();
  if (!id) return <p role="alert" className="p-4">Request id is missing.</p>;
  if (query.isError) return <div role="alert" className="p-4">
    <p>{query.error.message}</p>
    <p>The original {kind} reference is {id}. Its canonical request is unavailable.</p>
    <button type="button" className="min-h-11 px-3" onClick={() => void query.refetch()}>Retry</button>
  </div>;
  if (!query.data) return <p role="status" className="p-4">Resolving request…</p>;
  return <Navigate replace to={`${focusAskHref(query.data.ask.id)}${location.search}${location.hash}`} />;
}
