import { useState } from "react";
import { useAsks } from "../../api/asks";
import { AskCard } from "../../components/asks/AskCard";
import { useFocusChrome } from "./focusChrome";

export function AskInbox({ sourceKind }: { sourceKind?: string }) {
  const [view, setView] = useState<"needs_you" | "waiting" | "history">("needs_you");
  const [cursor, setCursor] = useState<string | undefined>();
  const query = useAsks({ view, source_kind: sourceKind, cursor });
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
        <div className="space-y-3">{query.data.items.map(ask => <AskCard key={ask.id} ask={ask} />)}</div>
        <p className="text-xs text-gray-500">As of {new Date(query.data.snapshot_at * 1000).toLocaleString()}</p>
        {query.data.next_cursor && <button className="min-h-11 px-3" onClick={() => setCursor(query.data.next_cursor!)}>Next page</button>}
        {cursor && <button className="min-h-11 px-3" onClick={() => setCursor(undefined)}>First page</button>}
      </>}
  </div>;
}
export default function FocusInbox() {
  useFocusChrome({ title: "Needs you" });
  return <AskInbox />;
}
