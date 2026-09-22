/** Compact account-wide weekly quota bars shown on every dashboard page. */

import { useProviderUsage, type ProviderUsageSnapshot } from "../api/providerUsage";
import {
  useProviderAvailability,
  type ProviderAvailabilityStatus,
} from "../api/providers";
import {
  isUnavailable,
  providerName,
  stateLabel,
} from "../pages/metrics/providerAvailabilityFormat";
import {
  formatAge,
  formatReset,
  selectWeeklySnapshots,
  toneFor,
} from "../pages/metrics/providerUsageFormat";

function isHeld(status: ProviderAvailabilityStatus | undefined): boolean {
  return Boolean(
    status &&
      (isUnavailable(status) || status.state === "disabled" || (status.held ?? 0) > 0),
  );
}

function availabilityDetail(status: ProviderAvailabilityStatus | undefined): string | null {
  if (!status || !isHeld(status)) return null;
  const reason = status.reason || status.override?.reason || status.reason_code;
  const heldTasks = (status.held ?? 0) > 0 ? `${status.held} tasks held` : null;
  return [stateLabel(status.state), reason, heldTasks].filter(Boolean).join(" · ");
}

function WeeklyUsageBar({
  row,
  now,
  status,
}: {
  row: ProviderUsageSnapshot;
  now: number;
  status: ProviderAvailabilityStatus | undefined;
}) {
  const percent = Number.isFinite(row.used_percent) ? row.used_percent : 0;
  const rounded = Math.round(percent);
  const clamped = Math.max(0, Math.min(100, percent));
  const stale = Boolean(row.stale);
  const held = isHeld(status);
  const tone = toneFor(percent, stale || held);
  const name = providerName(row.provider);
  const reset = formatReset(row.resets_at, now) ?? "reset time unavailable";
  const age = stale ? formatAge(row.age_seconds ?? 0) : null;
  const heldDetail = availabilityDetail(status);
  const stateDetail = [heldDetail, age ? `stale ${age}` : null].filter(Boolean).join(" · ");
  const title = [
    `${name}: ${rounded}% used this week`,
    reset,
    age ? `stale · last seen ${age}` : null,
    heldDetail,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      data-testid={`provider-weekly-${row.provider}`}
      data-stale={stale ? "true" : "false"}
      data-held={held ? "true" : "false"}
      data-state={status?.state ?? "unknown"}
      aria-label={`${name} weekly usage: ${rounded}%`}
      title={title}
      className={`flex min-w-0 items-center gap-1.5 rounded px-1.5 py-1 ${
        held ? "bg-gray-900/80 opacity-60" : "bg-gray-900/50"
      }`}
    >
      <span className="hidden shrink-0 text-[10px] font-medium text-gray-400 lg:inline">
        {name}
      </span>
      <div
        role="progressbar"
        aria-label={`${name} weekly usage used`}
        aria-valuenow={rounded}
        aria-valuemin={0}
        aria-valuemax={100}
        className="hidden h-1.5 w-14 shrink overflow-hidden rounded-full bg-gray-800 sm:block xl:w-20"
      >
        <div
          data-testid="weekly-usage-bar"
          className={`h-full rounded-full ${tone.bar} ${stale || held ? "opacity-50" : ""}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      <span className={`shrink-0 text-[11px] font-semibold tabular-nums ${tone.text}`}>
        {rounded}%
      </span>
      {stateDetail && (
        <span className="hidden max-w-24 truncate text-[10px] text-gray-500 lg:inline">
          {stateDetail}
        </span>
      )}
    </div>
  );
}

export default function ProviderUsageBars() {
  const usage = useProviderUsage();
  const availability = useProviderAvailability();
  if (!usage.data || usage.isError) return null;

  const rows = selectWeeklySnapshots(usage.data.snapshots ?? []);
  if (rows.length === 0) return null;

  const statusByProvider = new Map(
    (availability.data?.providers ?? []).map((status) => [status.provider, status] as const),
  );
  return (
    <div
      data-testid="provider-weekly-usage"
      aria-label="Weekly provider usage"
      className="flex min-w-0 flex-nowrap items-center justify-end gap-1 overflow-hidden sm:gap-2"
    >
      {rows.map((row) => (
        <WeeklyUsageBar
          key={row.provider}
          row={row}
          now={usage.data.now}
          status={statusByProvider.get(row.provider)}
        />
      ))}
    </div>
  );
}
