import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { XMarkIcon } from "@heroicons/react/24/outline";
import { useEventStream } from "../ws/useEventStream";
import { useEventBuffer } from "../ws/EventStreamProvider";
import type { NotifyEvent } from "../ws/types";

import { useRightSurface } from "./useRightSurface";
import { AskInbox } from "../pages/focus/FocusInbox";

function tabClass(active: boolean): string {
  return `rounded px-2 py-1 text-xs ${
    active ? "bg-indigo-500/20 text-indigo-200" : "text-gray-400 hover:bg-gray-800"
  }`;
}

function EventsList() {
  const [events, setEvents] = useState<Array<{ ts: number; event: NotifyEvent }>>([]);
  useEventStream({
    onEvent: (event) => {
      setEvents((prev) => [...prev.slice(-99), { ts: Date.now() / 1000, event }]);
    },
  });
  return (
    <ul className="divide-y divide-gray-800">
      {events.length === 0 && (
        <li className="p-3 text-xs text-gray-500">Waiting for events…</li>
      )}
      {events
        .slice()
        .reverse()
        .map((e, i) => (
          <li key={i} className="p-2 text-xs text-gray-300">
            <span className="font-mono text-gray-500">
              {new Date(e.ts * 1000).toLocaleTimeString()}
            </span>{" "}
            <span className="text-indigo-300">{e.event.event_type}</span>
          </li>
        ))}
    </ul>
  );
}

/**
 * An event's fields with its bus ``payload`` merged in: a live frame carries
 * them flattened, a replayed one nests them (as an object or a JSON string).
 */
function eventFields(event: NotifyEvent): Record<string, unknown> {
  const outer = event as unknown as Record<string, unknown>;
  let nested: unknown = outer.payload;
  if (typeof nested === "string") {
    try {
      nested = JSON.parse(nested);
    } catch {
      return outer;
    }
  }
  return nested && typeof nested === "object" && !Array.isArray(nested)
    ? { ...outer, ...(nested as Record<string, unknown>) }
    : outer;
}

function stringField(fields: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) if (typeof fields[key] === "string") return fields[key] as string;
  return "";
}

/**
 * The Events tab narrowed to one request (``?eventRequest=<id>``): the
 * Providers view links here with a provider allocation's request id, which
 * every underlying ``pool.*`` event carries.  Read from the app-wide event
 * buffer, so events that arrived before the drawer opened are listed too.
 */
function RequestEventsList({ requestId, onClear }: { requestId: string; onClear: () => void }) {
  const { events } = useEventBuffer();
  const matching = events
    .map((entry) => ({ entry, fields: eventFields(entry.event) }))
    .filter(({ fields }) => fields.request_id === requestId)
    .reverse();
  return (
    <div>
      <div className="flex items-center justify-between gap-2 border-b border-gray-800 px-2 py-1.5 text-xs text-gray-400">
        <span className="truncate">Request <code className="text-gray-200">{requestId}</code></span>
        <button type="button" aria-label="Clear request filter" onClick={onClear}
          className="rounded p-1 text-gray-500 hover:bg-gray-800">
          <XMarkIcon className="h-3.5 w-3.5" />
        </button>
      </div>
      <ul aria-label={"Events for " + requestId} className="divide-y divide-gray-800">
        {matching.length === 0 && (
          <li className="p-3 text-xs text-gray-500">
            No events for this request reached this browser. Pool changes stream here while the dashboard is open.
          </li>
        )}
        {matching.map(({ entry, fields }) => (
          <li key={entry.id} className="p-2 text-xs text-gray-300">
            <span className="font-mono text-gray-500">{entry.timestamp.toLocaleTimeString()}</span>{" "}
            <span className="text-indigo-300">{entry.event.event_type}</span>{" "}
            <span className="font-mono text-gray-400">{stringField(fields, "session_id", "profile_id", "provider")}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ActivityDrawer() {
  const { activityTab: tab, setActivityTab: setTab } = useRightSurface();
  const [params, setParams] = useSearchParams();
  const requestId = params.get("eventRequest");
  const clearRequest = () => {
    const next = new URLSearchParams(params);
    next.delete("eventRequest");
    setParams(next, { replace: true });
  };
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-gray-800 p-2">
        <button className={tabClass(tab === "gates")} onClick={() => setTab("gates")}>
          Needs you
        </button>
        <button className={tabClass(tab === "events")} onClick={() => setTab("events")}>
          Events
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {tab === "gates" ? <AskInbox />
          : requestId ? <RequestEventsList requestId={requestId} onClear={clearRequest} /> : <EventsList />}
      </div>
    </div>
  );
}
