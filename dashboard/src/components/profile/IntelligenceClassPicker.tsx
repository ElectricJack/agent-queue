import { useIntelligenceClasses, type IntelligenceClassRow as ClassRow } from "../../api/hooks";

const TIER_ORDER = ["fast", "standard", "deep"] as const;
const THINK_ORDER = ["off", "low", "medium", "high"] as const;

function tierOf(id: string): string {
  return id.split("-")[0] ?? id;
}
function thinkOf(id: string): string {
  return id.split("-")[1] ?? "";
}

function sortClasses(rows: ClassRow[]): ClassRow[] {
  return [...rows].sort((a, b) => {
    const ta = TIER_ORDER.indexOf(tierOf(a.id) as (typeof TIER_ORDER)[number]);
    const tb = TIER_ORDER.indexOf(tierOf(b.id) as (typeof TIER_ORDER)[number]);
    if (ta !== tb) return ta - tb;
    const ka = THINK_ORDER.indexOf(thinkOf(a.id) as (typeof THINK_ORDER)[number]);
    const kb = THINK_ORDER.indexOf(thinkOf(b.id) as (typeof THINK_ORDER)[number]);
    if (ka !== kb) return ka - kb;
    return a.id.localeCompare(b.id);
  });
}

interface Props {
  value: string;
  onChange: (next: string) => void;
}

export default function IntelligenceClassPicker({ value, onChange }: Props) {
  const { data, isLoading, error } = useIntelligenceClasses();
  const classes = sortClasses(data?.classes ?? []);
  const grouped = TIER_ORDER.map((tier) => ({
    tier,
    rows: classes.filter((c) => tierOf(c.id) === tier),
  }));
  const selected = classes.find((c) => c.id === value);

  if (isLoading) {
    return <p className="text-xs text-gray-500">Loading intelligence classes…</p>;
  }
  if (error) {
    return (
      <p className="text-xs text-red-400">
        Failed to load: {(error as Error).message}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <select
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
      >
        <option value="">— inherit / none —</option>
        {grouped.map(({ tier, rows }) =>
          rows.length === 0 ? null : (
            <optgroup key={tier} label={`${tier} tier`}>
              {rows.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id}
                </option>
              ))}
            </optgroup>
          ),
        )}
      </select>
      {selected && (
        <div className="rounded-md border border-gray-800 bg-gray-950/60 p-2 text-[11px] text-gray-500">
          <p className="mb-1 text-gray-400">{selected.description}</p>
          <ul className="space-y-0.5 font-mono">
            {Object.entries(selected.mapping)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([provider, slice]) => {
                const bits: string[] = [];
                if (slice.model) bits.push(slice.model);
                if (slice.thinking) bits.push(`think:${slice.thinking}`);
                if (slice.reasoning_effort) bits.push(`effort:${slice.reasoning_effort}`);
                if (typeof slice.thinking_budget === "number")
                  bits.push(`budget:${slice.thinking_budget}`);
                return (
                  <li key={provider}>
                    <span className="text-gray-400">{provider}:</span> {bits.join(" · ") || "—"}
                  </li>
                );
              })}
          </ul>
        </div>
      )}
    </div>
  );
}
