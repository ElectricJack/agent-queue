/**
 * Provider quota cards — what Anthropic and OpenAI say is left, not what we
 * counted.
 *
 * Token charts elsewhere on this tab measure our own spend; these cards
 * measure the provider's own limit windows, which is the number that decides
 * whether the fleet stalls at 4pm.  One card per ``(provider, window, scope)``
 * series, newest reading each.
 *
 * The one rule the whole file exists to enforce: **a frozen number must never
 * look live**.  A Codex percentage only advances while a Codex session is
 * writing transcript lines, and a Claude probe can quietly stop parsing when
 * the CLI's wording moves — in both cases the last reading keeps rendering
 * forever.  So a stale card says "stale · last seen 4h ago", mutes its bar and
 * drops its threshold colour, because an amber 88% that has not been confirmed
 * since breakfast is not the same fact as an amber 88% from ten minutes ago.
 *
 * Staleness itself is server-computed (``stale``/``age_seconds`` on the
 * response): the horizon differs per provider and the doctor check WARNs on the
 * same verdict, so deriving it a second time here is how two surfaces end up
 * disagreeing about one card.
 */

import { useProviderUsage } from "../../api/hooks";
import type { ProviderUsageSnapshot } from "../../api/hooks";
import { formatAge, formatReset, seriesLabel, sortSnapshots, toneFor } from "./providerUsage";

export function UsageCard({ row, now }: { row: ProviderUsageSnapshot; now: number }) {
  const percent = Number.isFinite(row.used_percent) ? row.used_percent : 0;
  const clamped = Math.max(0, Math.min(100, percent));
  const stale = Boolean(row.stale);
  const tone = toneFor(percent, stale);
  const label = seriesLabel(row);
  const reset = formatReset(row.resets_at, now);

  return (
    <div
      data-testid={`provider-card-${row.provider}-${row.window}-${row.scope ?? ""}`}
      data-stale={stale ? "true" : "false"}
      className={`rounded-xl border p-4 ${
        stale ? "border-gray-800 bg-gray-900/40" : "border-gray-800 bg-gray-900/70"
      }`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs uppercase tracking-wide text-gray-500">{label}</span>
        <span className={`text-xl font-semibold tabular-nums ${tone.text}`}>
          {Math.round(percent)}%
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${label} used`}
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        className="mt-3 h-2 w-full overflow-hidden rounded-full bg-gray-800"
      >
        <div
          data-testid="usage-bar"
          className={`h-full rounded-full ${tone.bar} ${stale ? "opacity-40" : ""}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      <p className="mt-2 truncate text-xs text-gray-500">
        {stale ? (
          <span className="text-amber-500/80">stale · last seen {formatAge(row.age_seconds ?? 0)}</span>
        ) : (
          (reset ?? "no reset time reported")
        )}
      </p>
      {stale && reset && <p className="mt-0.5 truncate text-[11px] text-gray-600">{reset}</p>}
    </div>
  );
}

export default function ProviderUsage() {
  const { data, isLoading, isError, error } = useProviderUsage();
  const rows = sortSnapshots(data?.snapshots ?? []);
  // The server's clock, so a reset time is not read against a skewed browser.
  const now = data?.now ?? Date.now() / 1000;

  return (
    <section className="space-y-2">
      <h2 className="text-xs uppercase tracking-wide text-gray-500">Provider limits</h2>
      {isError ? (
        <p className="rounded-lg border border-red-900/60 bg-red-950/40 p-3 text-sm text-red-200">
          Could not load provider usage: {String((error as Error)?.message ?? error)}
        </p>
      ) : isLoading ? (
        <p className="text-sm text-gray-500">Loading provider usage…</p>
      ) : rows.length === 0 ? (
        // Explicitly not a row of 0% bars: "nobody has reported yet" and
        // "you have used none of your quota" are different facts.
        <p className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 text-sm text-gray-500">
          No provider usage recorded yet
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {rows.map((row) => (
            <UsageCard key={row.id} row={row} now={now} />
          ))}
        </div>
      )}
    </section>
  );
}
