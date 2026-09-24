/**
 * One provider's availability, above its quota cards (provider-failover D20).
 *
 * State pill, reason, *since*, the countdown to expected recovery, the
 * operator override with its expiry and author, probation, the held and
 * re-routed counts, the remediation while unavailable — and the three
 * operator actions: *Disable…*, *Recheck*, *Clear override*.
 *
 * Nothing on this header is decided in the browser.  The pill is the
 * server's effective state, the override badge appears because the response
 * carries one, and after every action the availability query is invalidated
 * so the card shows what the daemon now holds rather than what we asked for.
 */

import { useState } from "react";
import {
  providerErrorText,
  useRecheckProvider,
  useSetProviderState,
  type ProviderAvailabilityStatus,
} from "../../api/providers";
import {
  DISABLE_DURATIONS,
  TONE_CLASSES,
  formatOverrideExpiry,
  formatRecovery,
  formatSince,
  isIndefinitelyDisabled,
  isUnavailable,
  probeLabel,
  providerName,
  stateLabel,
  stateTone,
} from "./providerAvailabilityFormat";

const actionButton =
  "rounded-md border border-gray-700 px-2 py-1 text-xs text-gray-200 hover:bg-gray-800 disabled:cursor-wait disabled:opacity-50";
const INDEFINITE_DURATION = "indefinite";

export default function ProviderAvailabilityHeader({
  status,
  now,
}: {
  status: ProviderAvailabilityStatus;
  /** The server clock, advanced locally (see ``useServerNow``). */
  now: number;
}) {
  const setState = useSetProviderState();
  const recheck = useRecheckProvider();
  const [disableOpen, setDisableOpen] = useState(false);
  const [duration, setDuration] = useState<string>(DISABLE_DURATIONS[0].key);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const key = status.provider;
  const name = providerName(key);
  const tone = TONE_CLASSES[stateTone(status.state)];
  const override = status.override ?? null;
  const indefinite = isIndefinitelyDisabled(status);
  const unavailable = isUnavailable(status);
  const since = formatSince(status.since, now);
  const recovery = indefinite ? null : formatRecovery(status.until, now);
  const reasonText = status.reason || status.reason_code || "";
  const pending = setState.isPending || recheck.isPending;
  const failure = setState.error ?? recheck.error;

  const openDisable = () => {
    setState.reset();
    setNotice(null);
    setReason("");
    setDisableOpen(true);
  };

  const submitDisable = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const why = reason.trim();
    // The daemon refuses an override without a reason (D6); saying so here
    // saves a round trip, it does not replace the server's check.
    if (!why) return;
    setState.mutate(
      duration === INDEFINITE_DURATION
        ? { provider: key, state: "disabled", no_expiry: true, reason: why }
        : { provider: key, state: "disabled", for: duration, reason: why },
      {
        onSuccess: () => {
          setDisableOpen(false);
          setNotice(duration === INDEFINITE_DURATION
            ? `${name} disabled indefinitely.`
            : `${name} disabled for ${duration}.`);
        },
      },
    );
  };

  const clearOverride = () => {
    setNotice(null);
    setState.mutate(
      { provider: key, state: "auto" },
      { onSuccess: () => setNotice("Override cleared; the state is re-derived from evidence.") },
    );
  };

  const runRecheck = () => {
    setNotice(null);
    recheck.mutate(key, {
      onSuccess: (result) =>
        setNotice(`Recheck: ${probeLabel(result.probe)} — now ${stateLabel(result.state)}.`),
    });
  };

  const detail = [reasonText, since].filter(Boolean).join(" · ");

  return (
    <div
      data-testid={`provider-availability-${key}`}
      data-state={status.state}
      data-half={status.half}
      className="space-y-2"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-gray-100">{name}</h3>
          {status.vendor && status.vendor !== key && (
            <span className="text-xs text-gray-500">{status.vendor}</span>
          )}
          <span
            data-testid={`provider-state-${key}`}
            className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${tone.pill}`}
          >
            <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
            {stateLabel(status.state)}
          </span>
          {status.probation && (
            <span
              data-testid={`provider-probation-${key}`}
              title="Recovering after an outage: work already routed here launches, but it is not a failover target until launches succeed again."
              className="rounded-full border border-amber-800/60 px-2 py-0.5 text-[11px] text-amber-300"
            >
              On probation
            </span>
          )}
          {override && (
            <span
              data-testid={`provider-override-${key}`}
              title={override.reason ? `Reason: ${override.reason}` : undefined}
              className="rounded-full border border-indigo-700/60 bg-indigo-500/10 px-2 py-0.5 text-[11px] text-indigo-200"
            >
              {indefinite ? (
                <>
                  disabled indefinitely
                  {override.reason ? ` · reason: ${override.reason}` : ""}
                  {override.by ? ` · by ${override.by}` : ""}
                </>
              ) : (
                <>
                  Override: {stateLabel(override.state).toLowerCase()} ·{" "}
                  {formatOverrideExpiry(override.until, now)}
                  {override.by ? ` · by ${override.by}` : ""}
                </>
              )}
            </span>
          )}
          {override && status.derived_state && status.derived_state !== status.state && (
            <span className="text-[11px] text-gray-500">
              evidence says {stateLabel(status.derived_state).toLowerCase()}
            </span>
          )}
          {status.mode && status.mode !== "enforce" && (
            <span
              title="Failover mode is not enforce: states are tracked, but nothing is held or moved."
              className="rounded-full border border-gray-700 px-2 py-0.5 text-[11px] text-gray-400"
            >
              mode: {status.mode}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={openDisable}
            disabled={pending}
            aria-expanded={disableOpen}
            className={actionButton}
          >
            Disable…
          </button>
          <button
            type="button"
            onClick={runRecheck}
            disabled={pending}
            aria-label={`Recheck ${name}`}
            className={actionButton}
          >
            {recheck.isPending ? "Rechecking…" : "Recheck"}
          </button>
          {override && (
            <button type="button" onClick={clearOverride} disabled={pending} className={actionButton}>
              Clear override
            </button>
          )}
        </div>
      </div>

      {(detail || recovery) && (
        <p className="text-xs text-gray-400">
          {detail}
          {detail && recovery ? " · " : null}
          {recovery && (
            <span data-testid={`provider-countdown-${key}`} className="text-gray-300">
              {recovery}
            </span>
          )}
        </p>
      )}

      <p className="text-xs text-gray-500">
        <span data-testid={`provider-held-${key}`}>
          <span className="tabular-nums text-gray-300">{status.held ?? 0}</span> held
        </span>
        {" · "}
        <span
          data-testid={`provider-rerouted-${key}`}
          title={status.batch_id ? `Batch ${status.batch_id}` : undefined}
        >
          <span className="tabular-nums text-gray-300">{status.rerouted ?? 0}</span> re-routed
        </span>
      </p>

      {unavailable && status.remediation && (
        <p
          data-testid={`provider-remediation-${key}`}
          className="whitespace-pre-wrap break-words rounded-md border border-red-900/50 bg-red-950/30 px-2 py-1.5 text-xs text-red-100"
        >
          {status.remediation}
        </p>
      )}

      {disableOpen && (
        <form
          aria-label={`Disable ${name}`}
          onSubmit={submitDisable}
          className="flex flex-wrap items-center gap-2 rounded-md border border-gray-800 bg-gray-950/60 p-2"
        >
          <label className="flex items-center gap-1.5 text-xs text-gray-400">
            Duration
            <select
              aria-label="Disable duration"
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
              className="rounded border border-gray-700 bg-gray-900 px-1.5 py-1 text-xs text-gray-200"
            >
              {DISABLE_DURATIONS.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.label}
                </option>
              ))}
              <option value={INDEFINITE_DURATION}>Until I re-enable it</option>
            </select>
          </label>
          <input
            type="text"
            aria-label="Reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why (required)"
            className="min-w-40 flex-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 placeholder:text-gray-600"
          />
          <button
            type="submit"
            disabled={!reason.trim() || setState.isPending}
            className="rounded-md bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
          >
            {setState.isPending ? "Disabling…" : "Disable"}
          </button>
          <button
            type="button"
            onClick={() => setDisableOpen(false)}
            className="rounded-md px-2 py-1 text-xs text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
        </form>
      )}

      {failure && (
        <p role="alert" className="text-xs text-red-300">
          {providerErrorText(failure)}
        </p>
      )}
      {notice && !failure && (
        <p role="status" className="text-xs text-gray-400">
          {notice}
        </p>
      )}
    </div>
  );
}
