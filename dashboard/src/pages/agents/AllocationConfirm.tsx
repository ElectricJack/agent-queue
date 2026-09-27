import { useId, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import type { ProviderAllocationPreviewResponse, ProviderAllocationProfileState } from "../../api/client";
import { providerName } from "../metrics/providerAvailabilityFormat";
import {
  affectedSessions,
  anyBusy,
  boundText,
  ceilingMax,
  interruptSet,
  plural,
  sessionActionText,
  unacknowledged,
} from "./allocation";

/** One compared field of a profile row; a pool's missing max is unbounded, a task profile has none. */
function fieldValue(state: ProviderAllocationProfileState, field: string): string {
  const value = (state as Record<string, unknown>)[field];
  if (field === "enabled") return value === false ? "disabled" : "enabled";
  if (value != null) return String(value);
  return field === "max_active" && state.lifecycle === "pool" ? "unbounded" : "—";
}

function preferenceName(provider: string | null | undefined): string {
  return provider ? providerName(provider) : "no preference";
}

/**
 * Step 2: the daemon's preview, confirmed as-is.
 *
 * Every row here is the preview's; apply sends back only its token (plus, for
 * an interrupt, the exact busy set the preview listed and, for pinned READY
 * work left waiting, the acknowledgement).  When any worker the request stops
 * is mid-task, the affected profiles and sessions open by default so the
 * operator reads them before confirming.  ``key`` this component by the
 * preview token: a fresh preview resets the typed confirmation.
 */
export default function AllocationConfirm({ preview, notice, pending, onBack, onApply }: {
  preview: ProviderAllocationPreviewResponse;
  notice: string | null;
  pending: boolean;
  onBack: () => void;
  onApply: (body: { authorize_busy_interrupt?: string[]; allow_pinned_wait?: boolean }) => void;
}) {
  const id = useId();
  const [typed, setTyped] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const expanded = anyBusy(preview);
  const changed = (preview.profiles ?? []).filter((row) => row.changed);
  const sessions = affectedSessions(preview);
  const busyCount = preview.busy?.session_ids?.length ?? 0;
  const interrupt = preview.request.drain === "interrupt-busy" ? interruptSet(preview) : [];
  const blocking = unacknowledged(preview);
  const warnings = preview.warnings ?? [];
  const pinned = preview.pinned ?? [];
  const manual = preview.manual_agents ?? [];
  const limits = preview.project_limits ?? [];
  const preference = preview.preference;
  const typedOk = interrupt.length === 0 || typed.trim() === String(interrupt.length);
  const canApply = !pending && typedOk && (blocking.length === 0 || acknowledged);

  const apply = () => {
    const body: { authorize_busy_interrupt?: string[]; allow_pinned_wait?: boolean } = {};
    if (interrupt.length > 0) body.authorize_busy_interrupt = interrupt.map((session) => session.session_id).sort();
    if (blocking.length > 0 && acknowledged) body.allow_pinned_wait = true;
    onApply(body);
  };

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-gray-100">Review before applying</h3>
      {notice && (
        <div role="alert" className="rounded border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-200">{notice}</div>
      )}
      {preview.required_scope === "operator" && (
        <p className="text-xs text-gray-500">Changes global profiles for every project: operator scope applies it.</p>
      )}

      {warnings.length > 0 && (
        <ul className="space-y-1.5">
          {warnings.map((warning) => (
            <li key={warning.code}
              className={"flex items-start gap-2 rounded border p-2 text-xs " + (warning.blocking && !warning.acknowledged
                ? "border-red-900 bg-red-950/30 text-red-200" : "border-gray-800 text-gray-300")}>
              <ExclamationTriangleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{warning.message}</span>
            </li>
          ))}
        </ul>
      )}
      {blocking.length > 0 && (
        <label className="flex items-start gap-2 text-sm text-gray-300">
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}
            className="mt-1 accent-red-500" />
          Leave the pinned READY tasks waiting on this provider: allocation never rewrites a pin.
        </label>
      )}

      <p className="text-sm text-gray-300">
        {`Provider ceiling ${ceilingMax(preview.ceiling.before)} → ${ceilingMax(preview.ceiling.after)} workers`}
        <span className="block text-xs text-gray-500">
          {`${plural(preview.ceiling.before.pool_profiles ?? 0, "pool profile")} before, ${preview.ceiling.after.pool_profiles ?? 0} after. `}
          Bounds are per profile; this total is for reading only.
        </span>
      </p>

      {changed.length > 0 && (
        <details open={expanded} className="rounded border border-gray-800 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-gray-300">{`Profiles (${changed.length} changing)`}</summary>
          <ul className="mt-2 space-y-1 text-xs text-gray-400">
            {changed.map((row) => (
              <li key={row.profile_id}>
                <span className="font-mono text-gray-200">{row.profile_id}</span>
                {" — "}
                {(row.changed_fields ?? []).map((field) => `${field} ${fieldValue(row.before, field)} → ${fieldValue(row.after, field)}`).join("; ")}
              </li>
            ))}
          </ul>
        </details>
      )}

      {(preview.sessions ?? []).length > 0 && (
        <details open={expanded} className="rounded border border-gray-800 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-gray-300">
            {`Sessions (${sessions.length} affected, ${busyCount} busy)`}
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-gray-400">
            {(preview.sessions ?? []).map((session) => (
              <li key={session.session_id} className={session.activity === "busy" ? "text-amber-200" : undefined}>
                <span className="font-mono">{session.session_id}</span>
                {` · ${session.project_id ?? "no project"} · ${session.activity}`}
                {session.task_id ? ` · ${session.task_id}${session.task_title ? " " + session.task_title : ""}` : ""}
                {" — "}{sessionActionText(session.action)}
              </li>
            ))}
          </ul>
        </details>
      )}

      {limits.length > 0 && (
        <details className="rounded border border-gray-800 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-gray-300">{`Project limits (${limits.length})`}</summary>
          <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-gray-400">
            {limits.map((limit) => (
              <li key={limit.project_id + "/" + limit.profile_id}>
                {`${limit.project_id} / ${limit.profile_id}: ${limit.lifecycle_before} ${boundText(limit.effective_max_before)} → ${limit.lifecycle_after} ${boundText(limit.effective_max_after)}`}
              </li>
            ))}
          </ul>
        </details>
      )}

      {pinned.length > 0 && (
        <div className="text-xs text-gray-400">
          <p className="font-medium text-gray-300">Pinned tasks left in place</p>
          <ul className="mt-1 space-y-0.5 font-mono text-[11px]">
            {pinned.map((task) => (
              <li key={task.task_id + "/" + task.profile_id}>
                {`${task.task_id} · ${task.status} · ${task.profile_id}${task.waits ? " · waits for this provider" : ""}`}
              </li>
            ))}
          </ul>
        </div>
      )}

      {manual.length > 0 && (
        <div className="text-xs text-gray-400">
          <p className="font-medium text-gray-300">Manual agents (never edited)</p>
          <ul className="mt-1 space-y-0.5">
            {manual.map((agent) => (
              <li key={agent.agent_id}>
                {`${agent.name ?? agent.agent_id} on ${agent.profile_id}: push work ${agent.push_before ? "yes" : "no"} → ${agent.push_after ? "yes" : "no"}`}
              </li>
            ))}
          </ul>
        </div>
      )}

      {preference && (
        <p className="text-xs text-gray-300">
          {`New unpinned work — ${preference.project_id}: ${preferenceName(preference.before)} → ${preferenceName(preference.after)}`}
          {!preference.changed && <span className="text-gray-500"> (already so)</span>}
        </p>
      )}

      {interrupt.length > 0 && (
        <fieldset className="space-y-2 rounded-md border border-red-800 bg-red-950/30 p-3">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-red-300">Interrupt busy work</legend>
          <p className="text-xs text-red-200">
            These workers are mid-task and stop now; their tasks are released through the normal session-stop path.
          </p>
          <ul className="space-y-0.5 font-mono text-[11px] text-red-100">
            {interrupt.map((session) => (
              <li key={session.session_id}>
                {`${session.session_id} · ${session.task_id ?? "no task"}${session.task_title ? " " + session.task_title : ""} · ${session.project_id ?? "no project"}`}
              </li>
            ))}
          </ul>
          <label htmlFor={id + "-typed"} className="block text-xs text-red-200">
            {`Type ${interrupt.length} to interrupt these busy tasks`}
          </label>
          <input id={id + "-typed"} value={typed} onChange={(event) => setTyped(event.target.value)} inputMode="numeric"
            autoComplete="off"
            className="w-24 rounded-md border border-red-800 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-200 focus:border-red-500 focus:outline-none" />
        </fieldset>
      )}

      <div className="flex items-center justify-between gap-2">
        <button type="button" onClick={onBack} disabled={pending}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800 disabled:opacity-40">
          Back to edit
        </button>
        <button type="button" disabled={!canApply} onClick={apply}
          className={"rounded px-3 py-2 text-sm font-medium text-white disabled:opacity-40 " + (interrupt.length > 0
            ? "bg-red-700 hover:bg-red-600" : "bg-indigo-600 hover:bg-indigo-500")}>
          {pending ? "Applying…" : interrupt.length > 0 ? `Interrupt ${plural(interrupt.length, "busy task")} and apply` : "Apply"}
        </button>
      </div>
    </div>
  );
}
