/**
 * The outage banner on every page (provider-failover D20).
 *
 * Shown while any provider's server-derived ``half`` is ``unavailable`` —
 * nothing launches against it, so work routed there is being moved or held,
 * and an operator on the Graph tab needs to know without visiting Metrics.
 * One line per unavailable provider: *"Codex unavailable — logged out since
 * 12:04 · 5 tasks moved · 4 held"*, linking to that provider's card.
 *
 * The browser never decides that a provider is down: the line exists because
 * the availability response says ``half: "unavailable"``, and it disappears
 * the poll (or WebSocket frame) after the server says otherwise.  A failed
 * read shows nothing rather than a stale alarm — the Metrics card reports
 * the read failure where there is room to explain it.
 */

import { Link } from "react-router-dom";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import { useProviderAvailability, type ProviderAvailabilityStatus } from "../api/providers";
import {
  TONE_CLASSES,
  formatClock,
  isIndefinitelyDisabled,
  isUnavailable,
  providerName,
  sortStatuses,
  stateTone,
  stateWords,
} from "../pages/metrics/providerAvailabilityFormat";

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function BannerLine({ status, now }: { status: ProviderAvailabilityStatus; now: number }) {
  const key = status.provider;
  const anchor = `provider-${key}`;
  const tone = TONE_CLASSES[stateTone(status.state)];
  const rerouted = status.rerouted ?? 0;
  const held = status.held ?? 0;
  const indefinite = isIndefinitelyDisabled(status);
  const override = status.override;
  const parts = [
    indefinite && override?.reason ? `reason: ${override.reason}` : null,
    indefinite && override?.by ? `by ${override.by}` : null,
    rerouted > 0 ? `${plural(rerouted, "task", "tasks")} moved` : null,
    held > 0 ? `${held} held` : null,
    !indefinite && status.until != null && status.until > now
      ? `expected back ${formatClock(status.until, now)}` : null,
  ].filter(Boolean);
  return (
    <div
      role="status"
      data-testid={`provider-banner-${key}`}
      data-state={status.state}
      className={`flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b px-4 py-1.5 text-xs ${tone.banner}`}
    >
      <ExclamationTriangleIcon aria-hidden="true" className="h-4 w-4 shrink-0" />
      <span className="font-medium">{providerName(key)} unavailable</span>{" "}
      <span>
        — {indefinite ? "disabled indefinitely" : stateWords(status.state)}
        {status.since != null ? ` since ${formatClock(status.since, now)}` : ""}
        {parts.length > 0 ? ` · ${parts.join(" · ")}` : ""}
      </span>{" "}
      <Link
        to={{ pathname: "/metrics", hash: anchor }}
        // Already on Metrics, the route does not change and nothing
        // re-renders, so scroll the card into view directly as well.
        onClick={() => {
          window.requestAnimationFrame?.(() =>
            document.getElementById(anchor)?.scrollIntoView?.({ block: "start", behavior: "smooth" }),
          );
        }}
        className="ml-auto underline underline-offset-2 opacity-90 hover:opacity-100"
      >
        View {providerName(key)}
      </Link>
    </div>
  );
}

export default function ProviderAvailabilityBanner() {
  const { data, isError } = useProviderAvailability();
  if (isError || !data) return null;
  const unavailable = sortStatuses(data.providers ?? []).filter(isUnavailable);
  if (unavailable.length === 0) return null;
  return (
    <div data-testid="provider-availability-banner">
      {unavailable.map((status) => (
        <BannerLine key={status.provider} status={status} now={data.now} />
      ))}
    </div>
  );
}
