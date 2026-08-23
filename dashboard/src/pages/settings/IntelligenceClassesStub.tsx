import { CpuChipIcon } from "@heroicons/react/24/outline";
import { useIntelligenceClasses, type IntelligenceClassRow } from "../../api/hooks";

type ProviderSlice = {
  model?: string;
  thinking?: string;
  reasoning_effort?: string;
  thinking_budget?: number;
  [k: string]: unknown;
};

const TIER_ORDER = ["fast", "standard", "deep"] as const;
const THINK_ORDER = ["off", "low", "medium", "high"] as const;

function tierOf(id: string): string {
  return id.split("-")[0] ?? id;
}
function thinkOf(id: string): string {
  return id.split("-")[1] ?? "";
}

function sortClasses(rows: IntelligenceClassRow[]): IntelligenceClassRow[] {
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

function providerBadge(name: string, slice: ProviderSlice): string {
  const bits: string[] = [];
  if (slice.model) bits.push(slice.model);
  if (slice.thinking) bits.push(`think:${slice.thinking}`);
  if (slice.reasoning_effort) bits.push(`effort:${slice.reasoning_effort}`);
  if (typeof slice.thinking_budget === "number") bits.push(`budget:${slice.thinking_budget}`);
  return `${name}: ${bits.join(" · ") || "—"}`;
}

export default function IntelligenceClassesStub() {
  const { data, isLoading, error } = useIntelligenceClasses();
  const classes = sortClasses(data?.classes ?? []);
  const grouped = TIER_ORDER.map((tier) => ({
    tier,
    rows: classes.filter((c) => tierOf(c.id) === tier),
  }));

  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold">Intelligence Classes</h2>
        <p className="text-sm text-gray-500">
          Curated model tiers referenced by task routing. Sourced from{" "}
          <code className="rounded bg-gray-800 px-1 text-xs">
            vault/intelligence-classes/*.md
          </code>
          .
        </p>
      </header>

      {isLoading && (
        <p className="text-sm text-gray-500">Loading intelligence classes…</p>
      )}
      {error && (
        <p className="text-sm text-red-400">
          Failed to load: {(error as Error).message}
        </p>
      )}

      {!isLoading && classes.length === 0 && (
        <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-10 text-center">
          <CpuChipIcon className="h-8 w-8 text-gray-600" />
          <p className="text-gray-400">
            No intelligence classes seeded. Restart the daemon to re-seed
            defaults from{" "}
            <code className="rounded bg-gray-800 px-1 text-xs">
              src/prompts/default_intelligence_classes/
            </code>
            .
          </p>
        </div>
      )}

      {grouped.map(({ tier, rows }) =>
        rows.length === 0 ? null : (
          <section key={tier}>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
              {tier} tier
            </h3>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {rows.map((cls) => (
                <div
                  key={cls.id}
                  className="rounded-lg border border-gray-800 bg-gray-900 p-3"
                >
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <p className="font-medium text-gray-100">{cls.name}</p>
                    <code className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-xs text-gray-400">
                      {cls.id}
                    </code>
                  </div>
                  {cls.description && (
                    <p className="mb-2 text-xs text-gray-400">{cls.description}</p>
                  )}
                  <ul className="space-y-0.5 text-xs text-gray-500">
                    {Object.entries(cls.mapping)
                      .sort(([a], [b]) => a.localeCompare(b))
                      .map(([provider, slice]) => (
                        <li key={provider} className="font-mono">
                          {providerBadge(provider, slice)}
                        </li>
                      ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        ),
      )}
    </div>
  );
}
