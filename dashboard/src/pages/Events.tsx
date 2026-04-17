import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { SignalIcon, TrashIcon, ArrowDownIcon } from "@heroicons/react/24/outline";
import {
  useEventStreamStatus,
  useEventBuffer,
} from "../ws/EventStreamProvider";
import type { NotifyEvent } from "../ws/types";

const severityColors: Record<string, string> = {
  info: "text-blue-400",
  warning: "text-yellow-400",
  error: "text-red-400",
  critical: "text-red-500",
};

const categoryColors: Record<string, string> = {
  task_lifecycle: "bg-indigo-500/10 text-indigo-400",
  interaction: "bg-purple-500/10 text-purple-400",
  vcs: "bg-cyan-500/10 text-cyan-400",
  budget: "bg-yellow-500/10 text-yellow-400",
  system: "bg-green-500/10 text-green-400",
  task_stream: "bg-gray-500/10 text-gray-400",
};

function formatTime(d: Date): string {
  return d.toLocaleTimeString("en-US", { hour12: false });
}

function eventSummary(event: NotifyEvent): string {
  switch (event.event_type) {
    case "notify.task_started":
      return `Task started: ${event.task.title}`;
    case "notify.task_completed":
      return `Task completed: ${event.task.title}${event.summary ? ` — ${event.summary}` : ""}`;
    case "notify.task_failed":
      return `Task failed: ${event.task.title}${event.error_detail ? ` — ${event.error_detail}` : ""}`;
    case "notify.task_blocked":
      return `Task blocked: ${event.task.title}${event.last_error ? ` — ${event.last_error}` : ""}`;
    case "notify.task_stopped":
      return `Task stopped: ${event.task.title}`;
    case "notify.agent_question":
      return `Agent question on ${event.task.title}: ${event.question}`;
    case "notify.plan_awaiting_approval":
      return `Plan awaiting approval: ${event.task.title} (${event.subtasks.length} subtasks)`;
    case "notify.pr_created":
      return `PR created for ${event.task.title}: ${event.pr_url}`;
    case "notify.merge_conflict":
      return `Merge conflict: ${event.task.title} (${event.branch} → ${event.target_branch})`;
    case "notify.push_failed":
      return `Push failed: ${event.task.title} ${event.branch}`;
    case "notify.budget_warning":
      return `Budget warning: ${event.project_name} at ${event.percentage.toFixed(0)}%`;
    case "notify.system_online":
      return "System online";
    case "notify.task_thread_open":
      return `Thread opened: ${event.thread_name}`;
    case "notify.task_message":
      return `[${event.task_id}] ${event.message}`;
    case "notify.task_thread_close":
      return `Thread closed: ${event.task_id} (${event.final_status})`;
    case "notify.text":
      return event.message;
    case "notify.chain_stuck":
      return `Chain stuck: ${event.stuck_task_titles.join(", ")}`;
    case "notify.stuck_defined_task":
      return `Stuck task: ${event.task.title} (${event.stuck_hours.toFixed(1)}h)`;
    default:
      return (event as NotifyEvent).event_type;
  }
}

function eventTaskId(event: NotifyEvent): string | null {
  if ("task" in event && event.task) return event.task.id;
  if ("task_id" in event && event.task_id) return event.task_id;
  return null;
}

const ALL_CATEGORIES = [
  "task_lifecycle",
  "interaction",
  "vcs",
  "budget",
  "system",
  "task_stream",
];

export default function Events() {
  const status = useEventStreamStatus();
  const { events, clearEvents } = useEventBuffer();
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState<Set<string>>(new Set(ALL_CATEGORIES));
  const [showMessages, setShowMessages] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const filteredEvents = events.filter((e) => {
    const cat = e.event.category ?? "system";
    if (!filter.has(cat)) return false;
    if (e.event.event_type === "notify.task_message" && !showMessages) return false;
    return true;
  });

  // Auto-scroll when new events arrive
  useEffect(() => {
    if (autoScroll) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [events, autoScroll]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setAutoScroll(atBottom);
  }, []);

  const toggleCategory = (cat: string) => {
    setFilter((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">Events</h1>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
              status === "connected"
                ? "bg-green-500/10 text-green-400"
                : status === "connecting"
                  ? "bg-yellow-500/10 text-yellow-400"
                  : "bg-red-500/10 text-red-400"
            }`}
          >
            <SignalIcon className="h-3 w-3" />
            {status}
          </span>
          <span className="text-sm text-gray-500">{filteredEvents.length} events</span>
        </div>
        <div className="flex items-center gap-2">
          {!autoScroll && (
            <button
              onClick={() => {
                setAutoScroll(true);
                bottomRef.current?.scrollIntoView({ behavior: "smooth" });
              }}
              className="flex items-center gap-1.5 rounded-lg border border-gray-700 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
            >
              <ArrowDownIcon className="h-3.5 w-3.5" />
              Follow
            </button>
          )}
          <button
            onClick={clearEvents}
            className="flex items-center gap-1.5 rounded-lg border border-gray-700 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
          >
            <TrashIcon className="h-3.5 w-3.5" />
            Clear
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {ALL_CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => toggleCategory(cat)}
            className={`rounded-full px-2.5 py-0.5 text-xs font-medium transition-opacity ${
              categoryColors[cat] ?? "bg-gray-500/10 text-gray-400"
            } ${filter.has(cat) ? "opacity-100" : "opacity-30"}`}
          >
            {cat.replace(/_/g, " ")}
          </button>
        ))}
        <label className="ml-2 flex items-center gap-1.5 text-xs text-gray-500">
          <input
            type="checkbox"
            checked={showMessages}
            onChange={(e) => setShowMessages(e.target.checked)}
            className="rounded border-gray-600 bg-gray-800"
          />
          task messages
        </label>
      </div>

      {/* Event list */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto rounded-lg border border-gray-800 bg-gray-950 font-mono text-sm"
      >
        {filteredEvents.length === 0 ? (
          <div className="flex h-full items-center justify-center text-gray-600">
            {status === "connected" ? "Waiting for events..." : "Connecting..."}
          </div>
        ) : (
          <div className="divide-y divide-gray-900">
            {filteredEvents.map((entry) => {
              const taskId = eventTaskId(entry.event);
              const severity = entry.event.severity ?? "info";
              const category = entry.event.category ?? "system";
              return (
                <div
                  key={entry.id}
                  className="px-4 py-3 hover:bg-gray-900/50"
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span className="text-xs text-gray-600">
                      {formatTime(entry.timestamp)}
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        categoryColors[category] ?? "bg-gray-500/10 text-gray-400"
                      }`}
                    >
                      {category.replace(/_/g, " ")}
                    </span>
                    <span
                      className={`text-xs font-medium ${severityColors[severity] ?? "text-gray-400"}`}
                    >
                      {entry.event.event_type.replace("notify.", "")}
                    </span>
                    {taskId && (
                      <Link
                        to={`/tasks/${taskId}`}
                        className="text-xs text-indigo-400 hover:underline"
                      >
                        {taskId}
                      </Link>
                    )}
                  </div>
                  <div className="whitespace-pre-wrap text-sm text-gray-300">
                    {eventSummary(entry.event)}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
