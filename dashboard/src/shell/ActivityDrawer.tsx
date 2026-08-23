import { useState } from "react";
import { CheckIcon, XMarkIcon } from "@heroicons/react/24/outline";
import {
  useAllOpenGates,
  useResolveGate,
  type GateSummary,
} from "../api/hooks";
import { useEventStream } from "../ws/useEventStream";
import type { NotifyEvent } from "../ws/types";
import { useShellPaneStore } from "../panes/store";
import { useListNav } from "./hotkeys/useListNav";

type Tab = "gates" | "events";

function tabClass(active: boolean): string {
  return `rounded px-2 py-1 text-xs ${
    active ? "bg-indigo-500/20 text-indigo-200" : "text-gray-400 hover:bg-gray-800"
  }`;
}

function GatesList() {
  const { data: gates, isLoading } = useAllOpenGates();
  const resolveMut = useResolveGate();
  const pane = useShellPaneStore();
  const listRef = useListNav<HTMLUListElement>({ axis: "vertical" });

  const openForGate = (g: GateSummary) => {
    const taskIds = (g as unknown as { task_ids?: string[] }).task_ids;
    const subjectId = (g as unknown as { subject_id?: string }).subject_id;
    if (g.gate_type === "routing" && subjectId) {
      pane.open("proposal-preview", { proposalId: subjectId });
      return;
    }
    if (taskIds && taskIds.length > 0) {
      pane.open("task-detail", { taskId: taskIds[0] });
    }
  };

  if (isLoading) return <p className="p-3 text-xs text-gray-500">Loading…</p>;
  if ((gates ?? []).length === 0)
    return <p className="p-3 text-xs text-gray-500">No open gates.</p>;

  return (
    <ul ref={listRef} className="divide-y divide-gray-800">
      {(gates ?? []).map((g) => (
        <li
          key={g.id}
          tabIndex={0}
          data-listnav="1"
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === "o") {
              e.preventDefault();
              openForGate(g);
            }
          }}
          className="cursor-pointer p-3 text-sm hover:bg-gray-900/40 focus:bg-gray-900/40 focus:outline-none"
          onClick={() => openForGate(g)}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="rounded bg-gray-800 px-1.5 py-0.5 text-[10px] uppercase text-gray-300">
                  {g.gate_type}
                </span>
                <span className="truncate text-gray-200">{g.title}</span>
              </div>
              {g.question && (
                <p className="mt-1 text-xs text-gray-400">{g.question}</p>
              )}
              <p className="mt-1 text-[10px] font-mono text-gray-600">
                {g.project_id}
              </p>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  resolveMut.mutate({
                    gate_id: g.id,
                    resolved_by: "dashboard",
                    resolution: "approved",
                  });
                }}
                className="rounded p-1 text-green-400 hover:bg-gray-800"
                title="Approve"
              >
                <CheckIcon className="h-4 w-4" />
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  resolveMut.mutate({
                    gate_id: g.id,
                    resolved_by: "dashboard",
                    resolution: "rejected",
                  });
                }}
                className="rounded p-1 text-red-400 hover:bg-gray-800"
                title="Reject"
              >
                <XMarkIcon className="h-4 w-4" />
              </button>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
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

export default function ActivityDrawer() {
  const [tab, setTab] = useState<Tab>("gates");
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-gray-800 p-2">
        <button className={tabClass(tab === "gates")} onClick={() => setTab("gates")}>
          Gates
        </button>
        <button className={tabClass(tab === "events")} onClick={() => setTab("events")}>
          Events
        </button>
      </div>
      <div className="flex-1 overflow-auto">
        {tab === "gates" ? <GatesList /> : <EventsList />}
      </div>
    </div>
  );
}
