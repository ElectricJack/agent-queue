import { useState } from "react";
import { useAsks } from "../../api/asks";
import { AskCard } from "./AskCard";

export function AskInbox({ sourceKind, newestFirst = false }: { sourceKind?: string; newestFirst?: boolean }) {
  const [view, setView] = useState<"needs_you" | "waiting" | "history">("needs_you");
  const [cursor, setCursor] = useState<string | undefined>();
  const query = useAsks({ view, source_kind: sourceKind, cursor });
  const items = newestFirst
    ? [...(query.data?.items ?? [])].sort(
        (a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || (b.created_at ?? 0) - (a.created_at ?? 0) || a.id.localeCompare(b.id),
      )
    : (query.data?.items ?? []);
  return <div className="space-y-3 p-3">
    <nav aria-label="Request views" className="flex flex-wrap gap-2">{([
      ["needs_you", "Needs you"], ["waiting", "Waiting on AQ"], ["history", "History"],
    ] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={view === value}
      className="min-h-11 rounded border border-gray-700 px-3 py-2" onClick={() => { setView(value); setCursor(undefined); }}>{label}</button>)}</nav>
    {query.isError ? <p role="alert">Requests could not be read. <button className="min-h-11 px-3" onClick={() => void query.refetch()}>Retry</button></p>
      : !query.data ? <p role="status">Loading requests…</p> : <>
        <p role="status">{view === "needs_you" ? "Needs you" : view === "waiting" ? "Waiting on AQ" : "History"}: {query.data.count}</p>
        {query.data.source_health !== "healthy" && <p role="status">{query.data.source_health} — this count may be incomplete. Refresh for current requests.</p>}
        {query.data.count === 0 && query.data.source_health === "healthy" && <p>No requests in this view.</p>}
        <div className="space-y-3">{items.map(ask => <AskCard key={ask.id} ask={ask} />)}</div>
        <p className="text-xs text-gray-500">As of {new Date(query.data.snapshot_at * 1000).toLocaleString()}</p>
        {query.data.next_cursor && <button className="min-h-11 px-3" onClick={() => setCursor(query.data.next_cursor!)}>Next page</button>}
        {cursor && <button className="min-h-11 px-3" onClick={() => setCursor(undefined)}>First page</button>}
      </>}
  </div>;
}
