/**
 * A task's provider story (provider-failover D17/D18/D20): why it is held,
 * where failover moved it from — with *undo* — and the intent chip.
 *
 * Every fact here arrives on the task response: ``provider_hold`` is the
 * daemon's derived hold (``null`` unless the task's provider is
 * unavailable), ``rerouted_from``/``reroute`` the re-route record with its
 * server-computed ``undoable``, ``provider_intent`` the stored intent.  The
 * strip renders them; it does not decide whether a task is held.
 */

import { useState } from "react";
import {
  providerErrorText,
  useProviderAvailability,
  useUndoReroute,
  type ProviderHoldDetail,
  type TaskReroute,
} from "../api/providers";
import {
  TONE_CLASSES,
  formatClock,
  formatRecovery,
  formatSince,
  holdKindLabel,
  intentHint,
  intentLabel,
  isUnavailable,
  providerName,
  stateLabel,
  stateTone,
} from "../pages/metrics/providerAvailabilityFormat";
import { useServerNow } from "../pages/metrics/useServerNow";

export interface ProviderRoutedTask {
  id: string;
  provider_intent?: string | null;
  rerouted_from?: string | null;
  reroute?: TaskReroute | null;
  provider_hold?: ProviderHoldDetail | null;
}

const INTENT_TONE: Record<string, string> = {
  pinned: "border-violet-700/60 bg-violet-500/15 text-violet-200",
  preferred: "border-sky-800/60 bg-sky-500/10 text-sky-300",
  class_only: "border-gray-700 bg-gray-800 text-gray-300",
};

/** *Pinned* / *Preferred* / *Class only* — the stored intent, never inferred. */
export function ProviderIntentChip({ intent }: { intent?: string | null }) {
  const key = intent || "class_only";
  return (
    <span
      data-testid="provider-intent-chip"
      data-intent={key}
      title={intentHint(key)}
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs ${INTENT_TONE[key] ?? INTENT_TONE.class_only}`}
    >
      {intentLabel(key)}
    </span>
  );
}

/** The hold, rendered like ``TaskAttention``: kind in words, then the provider's state. */
function ProviderHold({ hold }: { hold: ProviderHoldDetail }) {
  // The task response carries no server clock; the countdown is read
  // against the browser's, and the absolute time beside it is exact.
  const now = useServerNow(undefined, 0);
  const tone = TONE_CLASSES[stateTone(hold.state)];
  const since = formatSince(hold.since, now);
  const recovery = formatRecovery(hold.until, now);
  return (
    <section
      aria-label="Held by provider"
      data-testid="task-provider-hold"
      data-kind={hold.kind}
      className="rounded-lg border border-amber-700/50 bg-amber-950/20 p-3"
    >
      <h2 className="mb-1 text-sm font-semibold text-amber-300">Held by provider</h2>
      <p data-testid="task-provider-hold-kind" className="text-sm text-amber-100">
        {holdKindLabel(hold.kind, hold.ahead)}
      </p>
      <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-amber-200/80">
        <span className="font-medium text-amber-100">{providerName(hold.provider)}</span>
        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${tone.pill}`}>
          {stateLabel(hold.state)}
        </span>
        {hold.reason && <span>{hold.reason}</span>}
        {since && <span>{since}</span>}
        {recovery && <span data-testid="task-provider-hold-recovery">{recovery}</span>}
        {hold.profile_id && (
          <span>
            on <code className="font-mono">{hold.profile_id}</code>
          </span>
        )}
      </p>
      {hold.detail && <p className="mt-1 text-xs text-amber-200/70">{hold.detail}</p>}
      {hold.remediation && (
        <p
          data-testid="task-provider-hold-remediation"
          className="mt-2 whitespace-pre-wrap break-words rounded border border-amber-800/50 bg-gray-950/40 px-2 py-1.5 text-xs text-amber-100"
        >
          {hold.remediation}
        </p>
      )}
    </section>
  );
}

const REASON_WORDS: Record<string, string> = {
  provider_unavailable: "provider unavailable",
  operator_forced: "moved by an operator",
  operator_undo: "undone by an operator",
};

/** "re-routed from `standard-high-codex` · undo", with the refusal when there is one. */
function RerouteLine({
  taskId,
  from,
  reroute,
}: {
  taskId: string;
  from: string;
  reroute: TaskReroute | null;
}) {
  const undo = useUndoReroute();
  const [refusal, setRefusal] = useState<string | null>(null);
  // Only consulted after a refusal, to offer "Undo anyway" when the reason
  // is the one ``force`` overrides — the original provider still being
  // unavailable (D16).  Enabled lazily so an ordinary task view issues no
  // extra request; the banner keeps this query warm in the running app.
  const availability = useProviderAvailability({ enabled: !!refusal });
  const undoable = !!reroute?.undoable;
  const fromProvider = reroute?.from_provider ?? "";
  const homeUnavailable =
    !!fromProvider &&
    (availability.data?.providers ?? []).some((p) => p.provider === fromProvider && isUnavailable(p));

  const run = (force: boolean) => {
    setRefusal(null);
    undo.mutate(
      { task_id: [taskId], ...(force ? { force: true } : {}) },
      {
        onSuccess: (result) => {
          const mine = (result.refused ?? []).find((r) => r.task_id === taskId);
          if (mine) setRefusal(mine.reason);
        },
        onError: (error) => setRefusal(providerErrorText(error)),
      },
    );
  };

  const meta = [
    reroute?.from_provider && reroute?.to_provider
      ? `${reroute.from_provider} → ${reroute.to_provider}`
      : null,
    reroute?.reason_code ? (REASON_WORDS[reroute.reason_code] ?? reroute.reason_code) : null,
    reroute?.actor || null,
    reroute?.at != null ? formatClock(reroute.at, Date.now() / 1000) : null,
    reroute?.batch_id ? `batch ${reroute.batch_id}` : null,
  ].filter(Boolean);

  return (
    <section
      aria-label="Re-routed"
      data-testid="task-reroute"
      className="rounded-lg border border-sky-900/60 bg-sky-950/20 p-3 text-sm"
    >
      <p className="text-sky-100">
        Re-routed from <code className="font-mono text-sky-200">{from}</code>
        {reroute?.to_profile_id && (
          <>
            {" "}to <code className="font-mono text-sky-200">{reroute.to_profile_id}</code>
          </>
        )}
        {" · "}
        <button
          type="button"
          onClick={() => run(false)}
          disabled={!undoable || undo.isPending}
          title={
            undoable
              ? `Send the task back to ${from}`
              : "Not undoable now: the task is running or claimed, or the move was already undone."
          }
          className="text-sky-300 underline underline-offset-2 hover:text-sky-100 disabled:cursor-not-allowed disabled:text-gray-500 disabled:no-underline"
        >
          {undo.isPending ? "undoing…" : "undo"}
        </button>
      </p>
      {meta.length > 0 && <p className="mt-1 text-xs text-sky-200/60">{meta.join(" · ")}</p>}
      {refusal && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <p role="alert" className="text-xs text-red-300">
            Undo refused: {refusal}
          </p>
          {homeUnavailable && (
            <button
              type="button"
              onClick={() => run(true)}
              disabled={undo.isPending}
              className="rounded border border-red-800 px-2 py-0.5 text-xs text-red-200 hover:bg-red-950/60 disabled:opacity-50"
            >
              Undo anyway
            </button>
          )}
        </div>
      )}
    </section>
  );
}

export default function TaskProviderRouting({ task }: { task: ProviderRoutedTask }) {
  const hold = task.provider_hold ?? null;
  const from = task.rerouted_from ?? null;
  if (!hold && !from) return null;
  return (
    <div className="space-y-3">
      {hold && <ProviderHold hold={hold} />}
      {from && <RerouteLine taskId={task.id} from={from} reroute={task.reroute ?? null} />}
    </div>
  );
}
