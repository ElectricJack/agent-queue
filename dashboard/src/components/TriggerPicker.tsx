import { useMemo, useState } from "react";
import {
  XMarkIcon,
  HandRaisedIcon,
  BoltIcon,
  ClockIcon,
  CalendarDaysIcon,
} from "@heroicons/react/24/outline";
import { useEventTriggers } from "../api/hooks";

type TabId = "manual" | "events" | "cron" | "timer";

const TABS: { id: TabId; label: string; icon: typeof BoltIcon }[] = [
  { id: "manual", label: "Manual", icon: HandRaisedIcon },
  { id: "events", label: "Events", icon: BoltIcon },
  { id: "cron", label: "Cron", icon: CalendarDaysIcon },
  { id: "timer", label: "Timer", icon: ClockIcon },
];

interface Props {
  value: string[];
  onChange: (triggers: string[]) => void;
}

export default function TriggerPicker({ value, onChange }: Props) {
  const [tab, setTab] = useState<TabId>("events");

  const add = (t: string) => {
    if (value.includes(t)) return;
    onChange([...value, t]);
  };
  const remove = (t: string) => onChange(value.filter((x) => x !== t));

  return (
    <div className="space-y-3">
      {/* Selected chips */}
      <div className="flex min-h-[2rem] flex-wrap gap-1.5 rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5">
        {value.length === 0 ? (
          <span className="py-0.5 text-xs text-gray-500">No triggers yet</span>
        ) : (
          value.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1 rounded-full bg-indigo-500/15 px-2 py-0.5 text-xs font-medium text-indigo-300"
            >
              {t}
              <button
                onClick={() => remove(t)}
                className="rounded-full p-0.5 hover:bg-indigo-400/30"
                title="Remove trigger"
              >
                <XMarkIcon className="h-3 w-3" />
              </button>
            </span>
          ))
        )}
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b border-gray-800">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
              tab === id
                ? "border-b-2 border-indigo-400 text-indigo-400"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      {tab === "manual" && <ManualPanel value={value} add={add} remove={remove} />}
      {tab === "events" && <EventsPanel value={value} add={add} />}
      {tab === "cron" && <CronPanel add={add} />}
      {tab === "timer" && <TimerPanel add={add} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Manual — just a single toggle
// ---------------------------------------------------------------------------

function ManualPanel({
  value,
  add,
  remove,
}: {
  value: string[];
  add: (t: string) => void;
  remove: (t: string) => void;
}) {
  const active = value.includes("manual");
  return (
    <div className="rounded-md bg-gray-900 p-3 text-sm text-gray-300">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={active}
          onChange={(e) => (e.target.checked ? add("manual") : remove("manual"))}
        />
        <span>
          <span className="font-medium">manual</span> — only runs when invoked
          explicitly via <code className="text-gray-400">run_playbook</code>.
        </span>
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Events — filtered search + checkbox list, grouped by category
// ---------------------------------------------------------------------------

function EventsPanel({ value, add }: { value: string[]; add: (t: string) => void }) {
  const { data: events, isLoading } = useEventTriggers();
  const [query, setQuery] = useState("");

  const grouped = useMemo(() => {
    const filtered = (events ?? []).filter((e) =>
      query ? e.name.toLowerCase().includes(query.toLowerCase()) : true,
    );
    const byCategory = new Map<string, string[]>();
    for (const e of filtered) {
      const bucket = byCategory.get(e.category) ?? [];
      bucket.push(e.name);
      byCategory.set(e.category, bucket);
    }
    return Array.from(byCategory.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [events, query]);

  return (
    <div className="rounded-md bg-gray-900 p-3">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search events..."
        className="mb-3 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
      />
      {isLoading ? (
        <p className="text-xs text-gray-500">Loading events...</p>
      ) : grouped.length === 0 ? (
        <p className="text-xs text-gray-500">No events match.</p>
      ) : (
        <div className="max-h-56 overflow-y-auto pr-1">
          {grouped.map(([cat, names]) => (
            <div key={cat} className="mb-3 last:mb-0">
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-500">
                {cat}
              </div>
              <div className="space-y-0.5">
                {names.map((n) => (
                  <label
                    key={n}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-0.5 text-sm hover:bg-gray-800"
                  >
                    <input
                      type="checkbox"
                      checked={value.includes(n)}
                      onChange={(e) => {
                        if (e.target.checked) add(n);
                      }}
                      disabled={value.includes(n)}
                    />
                    <span className="font-mono text-xs text-gray-300">{n}</span>
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cron — HH:MM daily wall-clock picker
// ---------------------------------------------------------------------------

function CronPanel({ add }: { add: (t: string) => void }) {
  const [time, setTime] = useState("08:00");
  const valid = /^([01]\d|2[0-3]):[0-5]\d$/.test(time);
  return (
    <div className="space-y-2 rounded-md bg-gray-900 p-3">
      <p className="text-xs text-gray-500">
        Fires once per local day at the given wall-clock time.
      </p>
      <div className="flex items-center gap-2">
        <input
          type="time"
          value={time}
          onChange={(e) => setTime(e.target.value)}
          className="rounded-md border border-gray-700 bg-gray-950 px-3 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
        <span className="font-mono text-xs text-gray-500">→ cron.{time}</span>
        <button
          onClick={() => add(`cron.${time}`)}
          disabled={!valid}
          className="ml-auto rounded-md bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
        >
          Add
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Timer — periodic N minutes / hours
// ---------------------------------------------------------------------------

const TIMER_PRESETS = ["5m", "15m", "30m", "1h", "4h", "12h", "24h"];

function TimerPanel({ add }: { add: (t: string) => void }) {
  const [amount, setAmount] = useState(5);
  const [unit, setUnit] = useState<"m" | "h">("m");

  const value = `${amount}${unit}`;
  const valid = amount >= 1 && amount <= 99;

  return (
    <div className="space-y-3 rounded-md bg-gray-900 p-3">
      <p className="text-xs text-gray-500">
        Fires periodically on elapsed time since the last tick.
      </p>

      <div className="flex flex-wrap gap-1.5">
        {TIMER_PRESETS.map((p) => (
          <button
            key={p}
            onClick={() => add(`timer.${p}`)}
            className="rounded-full bg-gray-800 px-2.5 py-0.5 text-xs text-gray-300 hover:bg-indigo-500/20 hover:text-indigo-300"
          >
            timer.{p}
          </button>
        ))}
      </div>

      <div className="flex items-center gap-2 border-t border-gray-800 pt-2">
        <input
          type="number"
          min={1}
          max={99}
          value={amount}
          onChange={(e) => setAmount(parseInt(e.target.value, 10) || 1)}
          className="w-16 rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
        <select
          value={unit}
          onChange={(e) => setUnit(e.target.value as "m" | "h")}
          className="rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        >
          <option value="m">minutes</option>
          <option value="h">hours</option>
        </select>
        <span className="font-mono text-xs text-gray-500">→ timer.{value}</span>
        <button
          onClick={() => add(`timer.${value}`)}
          disabled={!valid}
          className="ml-auto rounded-md bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
        >
          Add
        </button>
      </div>
    </div>
  );
}
