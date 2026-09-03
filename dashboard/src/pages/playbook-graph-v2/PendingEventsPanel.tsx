type Pending = { pending_event_id: string; event_type: string; received_at: number; reason: string; attempts?: number };

// `received_at` is epoch seconds (PendingEventDTO.received_at).  Age is what an
// operator triages on — "how long has this been stuck?" — so render the elapsed
// time and keep the absolute timestamp on the title for the exact answer.
export function formatAge(receivedAt: number, now: number = Date.now() / 1000): string {
  const seconds = Math.max(0, Math.floor(now - receivedAt));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export default function PendingEventsPanel({ events, onAction }: { events: Pending[]; onAction: (action: "dispatch" | "discard", ids: string[]) => void }) { return <section aria-label="Pending events" className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"><h2 className="font-medium text-gray-100">Pending events</h2>{events.length === 0 ? <p className="text-sm text-gray-500">No pending events.</p> : events.map((event) => <div key={event.pending_event_id} className="flex flex-wrap items-center gap-2 border-t border-gray-800 pt-2 text-sm"><span className="font-mono text-gray-300">{event.event_type}</span><span className="text-amber-300">{event.reason.replace(/_/g, " ")}</span><time dateTime={new Date(event.received_at * 1000).toISOString()} title={new Date(event.received_at * 1000).toISOString()} className="text-xs text-gray-400">{formatAge(event.received_at)} old</time><span className="text-xs text-gray-500">{event.attempts ?? 0} attempts</span><button type="button" aria-label={`Dispatch event ${event.pending_event_id}`} onClick={() => onAction("dispatch", [event.pending_event_id])} className="rounded bg-indigo-700 px-2 py-1 text-xs">Dispatch</button><button type="button" aria-label={`Discard event ${event.pending_event_id}`} onClick={() => onAction("discard", [event.pending_event_id])} className="rounded bg-gray-700 px-2 py-1 text-xs">Discard</button></div>)}</section>; }
