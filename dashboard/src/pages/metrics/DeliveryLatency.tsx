/**
 * Delivery latency — how long completed work takes to reach dev (spec §10).
 *
 * One completion cohort from ``GET /api/metrics/delivery-latency``.  Three
 * rules shape every number here:
 *
 * - **Percentiles exclude pending work.**  The median and p90 cover delivered
 *   samples only, so the sample, eligible, pending and unknown counts sit
 *   right beside them.  A fast median over a quarter of the cohort is not a
 *   fast cohort.
 * - **Null is no data, never zero.**  Every missing figure renders "No data".
 * - **Timing is history; delivery is now.**  A sample whose dev ref was
 *   rewritten keeps its timing and also shows the current blocker.  The
 *   daemon's own deployment is a third fact: being on dev does not mean
 *   being deployed, and the comparison here is by time, not by commit.
 *
 * Waits are not re-explained here: a generation links to the shared wait
 * explanation (``TaskWaitPanel``), the project wait graph, the task's explain
 * view and the exact CI run each wait recorded.
 */

import { Fragment, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useDeploymentHistory, useProjects } from "../../api/hooks";
import {
  DELIVERY_WINDOWS,
  useDeliveryLatency,
  type DeliveryWindow,
} from "../../api/metrics";
import type {
  DeliveryLatencyGeneration,
  DeliveryLatencyResponse,
  DeliveryLatencyWait,
  DeploymentEvent,
} from "../../api/client";
import TaskWaitPanel from "../../components/TaskWaitPanel";
import {
  STALE_AFTER_SECONDS,
  deliveryState,
  deployState,
  formatDuration,
  formatPercent,
  type DeliveryState,
  type DeployState,
} from "./deliveryLatencyFormat";
import { useServerNow } from "./useServerNow";

const SEGMENT_LABEL: Record<string, string> = {
  slot_wait: "Slot wait",
  ci_time: "CI time",
  train_wait: "Train wait",
  repair: "Repair",
  author_merge: "Author merge",
};

const EXCLUSION_LABEL: Record<string, string> = {
  delivered_by_other_means: "delivered by other means",
  code_free: "code-free",
  abandoned: "abandoned",
  superseded: "superseded",
  settled: "settled",
};

function formatTime(ts: number | null | undefined): string {
  return ts == null ? "—" : new Date(ts * 1000).toLocaleString();
}

const shortOid = (oid: string | null | undefined) => (oid ? oid.slice(0, 9) : null);

const STATE_BADGE: Record<DeliveryState, { label: string; className: string }> = {
  on_dev: { label: "on dev", className: "bg-emerald-500/15 text-emerald-300" },
  current_blocker: { label: "dev no longer contains it", className: "bg-red-500/15 text-red-300" },
  pending: { label: "pending", className: "bg-sky-500/15 text-sky-300" },
  slow: { label: "slow", className: "bg-amber-500/15 text-amber-300" },
  stuck: { label: "stuck", className: "bg-red-500/15 text-red-300" },
  unknown: { label: "unknown", className: "bg-gray-700/60 text-gray-300" },
  historical_unknown: { label: "historical unknown", className: "bg-gray-700/60 text-gray-300" },
  left_censored: { label: "left-censored", className: "bg-gray-700/60 text-gray-300" },
  excluded: { label: "excluded", className: "bg-gray-800 text-gray-400" },
};

const DEPLOY_LABEL: Record<Exclude<DeployState, null>, string> = {
  deployed_after: "daemon deployed after on-dev (by time, not commit)",
  awaiting: "no daemon deploy since on-dev",
  unknown: "deploy status unknown",
};

function Tile({ label, value, note, testId }: {
  label: string; value: string; note?: ReactNode; testId?: string;
}) {
  return (
    <div data-testid={testId} className="rounded-xl border border-gray-800 bg-gray-900/70 p-3">
      <p className="text-[11px] uppercase tracking-wide text-gray-500">{label}</p>
      <p className={`mt-1 text-lg font-semibold tabular-nums ${value === "No data" ? "text-gray-500" : "text-gray-100"}`}>
        {value}
      </p>
      {note && <div className="mt-1 text-xs text-gray-500">{note}</div>}
    </div>
  );
}

function Headline({ report }: { report: DeliveryLatencyResponse }) {
  const target = report.target;
  const unknown = (report.unknown_count ?? 0) + (report.historical_unknown_count ?? 0);
  const bias = (
    <p data-testid="delivery-bias" className="text-amber-300/90">
      Percentiles exclude {report.pending_count ?? 0} pending and {unknown} unknown —
      sample {report.sample_count ?? 0} of {report.eligible_count ?? 0} eligible
      ({formatPercent(report.coverage)} coverage)
    </p>
  );
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Tile testId="delivery-median" label="Median approval → dev"
            value={formatDuration(report.median_seconds)} note={bias} />
      <Tile testId="delivery-p90" label="p90 approval → dev" value={formatDuration(report.p90_seconds)}
            note="Reported only; no p90 target" />
      <Tile label="Completion → dev" value={formatDuration(report.completion_to_dev_median_seconds)}
            note={`p90 ${formatDuration(report.completion_to_dev_p90_seconds)}`} />
      <Tile label="Within target" value={formatPercent(report.within_target_fraction)}
            note={target?.target_seconds != null
              ? `target ${formatDuration(target.target_seconds)} (${target.source === "floor" ? "floor — CI not yet measured" : `${target.ci_multiplier ?? "?"}× p90 batch CI`})`
              : "no target"} />
      <Tile testId="delivery-pending" label="Pending" value={String(report.pending_count ?? 0)}
            note={`oldest ${formatDuration(report.oldest_pending_age_seconds)}`} />
      <Tile testId="delivery-watchdog" label="Slow / stuck"
            value={`${report.slow_count ?? 0} / ${report.stuck_count ?? 0}`}
            note="slow has a live wait; stuck has none" />
      <Tile testId="delivery-unknown" label="Unknown" value={String(report.unknown_count ?? 0)}
            note={`${report.historical_unknown_count ?? 0} historical · ${report.left_censored_count ?? 0} left-censored · ${formatPercent(report.unknown_fraction)} of cohort`} />
      <Tile label="Current blockers" value={String(report.current_blocker_count ?? 0)}
            note={`timing kept after a ref rewrite · ${report.late_sample_count ?? 0} late · ${report.clock_skew_count ?? 0} clock skew`} />
    </div>
  );
}

function Breakdown({ report }: { report: DeliveryLatencyResponse }) {
  const excluded = (report.excluded ?? []).filter((e) => (e.count ?? 0) > 0);
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      <section aria-label="Time breakdown" className="rounded-xl border border-gray-800 bg-gray-900/70 p-3">
        <h3 className="text-xs font-semibold uppercase text-gray-500">Time breakdown (cohort medians)</h3>
        <p className="mt-1 text-xs text-gray-500">Segments overlap and do not sum to the total.</p>
        <table className="mt-2 w-full text-sm">
          <tbody>
            {(report.segments ?? []).map((segment) => (
              <tr key={segment.segment} className="border-t border-gray-800">
                <td className="py-1 text-gray-300">{SEGMENT_LABEL[segment.segment] ?? segment.segment}</td>
                <td className="py-1 text-right tabular-nums text-gray-200">{formatDuration(segment.median_seconds)}</td>
                <td className="py-1 pl-3 text-right text-xs text-gray-500">
                  {segment.generations ?? 0} gen
                  {segment.segment === "ci_time" && (
                    <span data-testid="delivery-cache">
                      {" "}· cache hit {formatPercent(report.cache_hit_rate)}
                      {report.cache_hit_rate_derived && " (derived)"} · rebuild {formatDuration(report.rebuild_seconds_total)}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            <tr className="border-t border-gray-800">
              <td className="py-1 text-gray-300">Residual</td>
              <td className="py-1 text-right tabular-nums text-gray-200">{formatDuration(report.residual_median_seconds)}</td>
              <td className="py-1 pl-3 text-right text-xs text-gray-500">not covered by any segment</td>
            </tr>
          </tbody>
        </table>
      </section>
      <section aria-label="Delivery diagnostics" className="space-y-2 rounded-xl border border-gray-800 bg-gray-900/70 p-3 text-sm">
        <h3 className="text-xs font-semibold uppercase text-gray-500">Diagnostics</h3>
        <p className="text-gray-300">Parent delay (median): {formatDuration(report.parent_delay_median_seconds)}</p>
        <p className="text-gray-300">Author-assisted close (median): {formatDuration(report.author_merge_median_seconds)}</p>
        <p data-testid="delivery-excluded" className="text-gray-300">
          Excluded: {excluded.length === 0 ? "none"
            : excluded.map((e) => `${e.count} ${EXCLUSION_LABEL[e.reason] ?? e.reason}`).join(" · ")}
        </p>
        {(report.batches_per_day ?? []).length > 0 && (
          <p className="text-gray-300">
            Batches per day:{" "}
            {(report.batches_per_day ?? []).map((d) =>
              `${new Date(d.day * 1000).toLocaleDateString()} ${d.batches ?? 0} (${d.members ?? 0} members, ${d.red_rounds ?? 0} red)`).join(" · ")}
          </p>
        )}
        {(report.historical_age_histogram ?? []).length > 0 && (
          <p className="text-gray-300">
            Historical backlog by age:{" "}
            {(report.historical_age_histogram ?? []).map((b) => `${b.bucket} ${b.count ?? 0}`).join(" · ")}
          </p>
        )}
      </section>
    </div>
  );
}

function WaitRow({ wait }: { wait: DeliveryLatencyWait }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-xs text-gray-400">
      <span className="font-mono text-gray-300">{wait.condition_kind}</span>
      {wait.segment && <span className="text-gray-500">{SEGMENT_LABEL[wait.segment] ?? wait.segment}</span>}
      <span className={wait.outcome === "pending" ? "text-sky-300" : "text-gray-500"}>{wait.outcome}</span>
      <span>{formatTime(wait.created_at)} → {wait.resolved_at ? formatTime(wait.resolved_at) : "open"}</span>
      {wait.decision_reason && <span className="text-gray-300">reason: {wait.decision_reason}</span>}
      {wait.evidence_ref && <span className="font-mono">{wait.evidence_ref}</span>}
      {wait.ci_url && (
        <a href={wait.ci_url} target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-0.5 text-indigo-400 hover:underline">
          CI run <ArrowTopRightOnSquareIcon className="h-3 w-3" />
        </a>
      )}
    </li>
  );
}

function GenerationDetail({ g, deploy }: { g: DeliveryLatencyGeneration; deploy: DeployState }) {
  const segments = Object.entries(g.segments ?? {});
  return (
    <div className="space-y-2 p-3 text-xs text-gray-400">
      <p data-testid="generation-refs">
        <span className="font-mono">{g.source_ref ?? "unknown source"}{g.source_oid && `@${shortOid(g.source_oid)}`}</span>
        {" → "}
        <span className="font-mono">{g.dev_ref ?? "dev"}{g.target_oid && `@${shortOid(g.target_oid)}`}</span>
        {g.reason && <> · Git reason <span className="text-gray-200">{g.reason}</span></>}
        {" · "}last Git transition {formatTime(g.observed_at)} (written on change; not a freshness probe)
      </p>
      {g.current_blocker && (
        <p className="text-red-300">
          Timing kept from the first sighting; dev no longer contains this generation (ref rewrite).
          Current delivery is blocked until it lands again.
        </p>
      )}
      <p>
        Completed {formatTime(g.completed_at)} · anchor {g.anchor_kind ?? "—"} {formatTime(g.anchor_at)}
        {" · "}on dev {formatTime(g.on_dev_at)}
        {g.parent_ref && <> · parent {g.parent_ref} {formatTime(g.parent_at)}</>}
        {deploy && <> · <span data-testid="generation-deploy">{DEPLOY_LABEL[deploy]}</span></>}
      </p>
      <p>
        Human wait {formatDuration(g.human_wait_seconds)} · residual {formatDuration(g.residual_seconds)}
        {segments.length > 0 && <> · {segments.map(([k, v]) => `${SEGMENT_LABEL[k] ?? k} ${formatDuration(v)}`).join(" · ")}</>}
        {g.rebuild_seconds != null && <> · rebuild {formatDuration(g.rebuild_seconds)}</>}
        {g.clock_skew && <span className="text-amber-300"> · clock skew (capped at zero)</span>}
      </p>
      {(g.waits ?? []).length > 0 ? (
        <ul aria-label="Linked waits" className="space-y-1">
          {(g.waits ?? []).map((w) => <WaitRow key={w.wait_id} wait={w} />)}
        </ul>
      ) : (
        <p>No linked wait records.</p>
      )}
      <div className="flex flex-wrap gap-3">
        <Link to={`/tasks/${encodeURIComponent(g.task_id)}?tab=explain`} className="text-indigo-400 hover:underline">
          Task explain
        </Link>
        {g.project_id && (
          <Link to={`/waiting?project=${encodeURIComponent(g.project_id)}`} className="text-indigo-400 hover:underline">
            Project wait graph
          </Link>
        )}
      </div>
      {g.project_id && <TaskWaitPanel taskId={g.task_id} projectId={g.project_id} />}
    </div>
  );
}

function GenerationTable({ report, history, taskFilter }: {
  report: DeliveryLatencyResponse; history: DeploymentEvent[] | undefined; taskFilter: string | null;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const all = report.generations ?? [];
  const rows = taskFilter ? all.filter((g) => g.task_id === taskFilter) : all;
  if (all.length === 0) {
    return <p className="text-sm text-gray-500">No completed generations in this cohort.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-800">
      {taskFilter && (
        <p className="border-b border-gray-800 px-3 py-2 text-xs text-gray-400">
          Showing {taskFilter} only — {rows.length === 0 ? "not in this cohort's drill-down" : `${rows.length} generation(s)`}.
        </p>
      )}
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Generations, pending first</caption>
        <thead className="text-xs text-gray-500">
          <tr>
            <th className="px-3 py-2">Task / generation</th><th className="px-3 py-2">State</th>
            <th className="px-3 py-2">Approval → dev</th><th className="px-3 py-2">Age</th>
            <th className="px-3 py-2">Deploy</th><th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => {
            const key = `${g.task_id}:${g.generation_id}`;
            const state = deliveryState(g);
            const deploy = deployState(g, history);
            const expanded = open === key || (taskFilter != null && rows.length === 1);
            return (
              <Fragment key={key}>
                <tr data-testid={`generation-${g.task_id}`} data-state={state} className="border-t border-gray-800">
                  <td className="px-3 py-2">
                    <Link to={`/tasks/${encodeURIComponent(g.task_id)}?tab=explain`} className="text-indigo-300 hover:underline">
                      {g.task_id}
                    </Link>
                    <span className="ml-2 font-mono text-[11px] text-gray-500">{g.generation_id}</span>
                  </td>
                  <td className="px-3 py-2">
                    <span className={`rounded px-2 py-0.5 text-xs ${STATE_BADGE[state].className}`}>
                      {STATE_BADGE[state].label}
                      {state === "excluded" && g.exclusion && `: ${EXCLUSION_LABEL[g.exclusion] ?? g.exclusion}`}
                    </span>
                    {state === "current_blocker" && <span className="ml-1 text-[11px] text-gray-500">timing kept</span>}
                    {g.late && <span className="ml-1 text-[11px] text-gray-500">late</span>}
                  </td>
                  <td className="px-3 py-2 tabular-nums">{g.approval_to_dev_seconds != null ? formatDuration(g.approval_to_dev_seconds) : "—"}</td>
                  <td className="px-3 py-2 tabular-nums">{g.age_seconds != null ? formatDuration(g.age_seconds) : "—"}</td>
                  <td className="px-3 py-2 text-xs text-gray-400">{deploy ? DEPLOY_LABEL[deploy] : "—"}</td>
                  <td className="px-3 py-2 text-right">
                    <button type="button" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : key)}
                            className="text-xs text-indigo-400 hover:underline">
                      {expanded ? "Hide" : "Details"}
                    </button>
                  </td>
                </tr>
                {expanded && (
                  <tr className="bg-gray-950/60"><td colSpan={6}><GenerationDetail g={g} deploy={deploy} /></td></tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LastDeploy({ history, error }: { history: DeploymentEvent[] | undefined; error: boolean }) {
  if (error) return <span>Daemon deploy history unavailable</span>;
  const last = history?.length ? history[history.length - 1] : undefined;
  if (!last) return <span>No daemon deployments recorded</span>;
  return (
    <span data-testid="delivery-last-deploy">
      Last daemon deploy {last.outcome.replace(/_/g, " ")} {last.target?.slice(0, 9) || ""} at {formatTime(last.timestamp)}
      {" "}— deploy state is separate from on-dev
    </span>
  );
}

export default function DeliveryLatency() {
  const [params, setParams] = useSearchParams();
  const [cohort, setCohort] = useState<DeliveryWindow>("24h");
  const projectId = params.get("project") || undefined;
  const taskFilter = params.get("task");
  const { data: report, error, isLoading, dataUpdatedAt } = useDeliveryLatency(cohort, projectId);
  const { data: projects } = useProjects();
  const deploys = useDeploymentHistory();
  const now = useServerNow(report?.observed_at, dataUpdatedAt);
  const age = report ? now - report.observed_at : 0;
  const stale = report != null && (error != null || age > STALE_AFTER_SECONDS);
  const projectOptions = useMemo(() => (projects ?? []).map((p) => ({ id: p.id, name: p.name ?? p.id })), [projects]);

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <section id="delivery" aria-label="Delivery latency" className="space-y-3">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-100">Delivery latency</h2>
          <p className="text-xs text-gray-500">
            {report ? (
              <>
                {cohort} completion cohort · observed {formatTime(report.observed_at)}
                {stale && (
                  <span data-testid="delivery-stale" className="ml-1 rounded bg-amber-500/15 px-1.5 text-amber-300">
                    stale · {error ? "refresh failed" : `${formatDuration(age)} old`}
                  </span>
                )}
                {" · "}<LastDeploy history={deploys.data} error={deploys.isError} />
              </>
            ) : "Completion to first sight on dev"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select aria-label="Delivery project" value={projectId ?? ""}
                  onChange={(e) => setParam("project", e.target.value || null)}
                  className="rounded-lg border border-gray-800 bg-gray-900 px-2 py-1.5 text-xs text-gray-200">
            <option value="">All projects</option>
            {projectOptions.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <div role="group" aria-label="Delivery cohort" className="flex overflow-hidden rounded-lg border border-gray-800">
            {DELIVERY_WINDOWS.map((key) => (
              <button key={key} type="button" onClick={() => setCohort(key)} aria-pressed={cohort === key}
                      className={`px-3 py-1.5 text-xs ${cohort === key ? "bg-indigo-500/20 text-indigo-200" : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"}`}>
                {key}
              </button>
            ))}
          </div>
          {taskFilter && (
            <button type="button" onClick={() => setParam("task", null)} className="text-xs text-indigo-400 hover:underline">
              Clear task filter
            </button>
          )}
        </div>
      </header>

      {isLoading && <p className="text-sm text-gray-500">Loading delivery latency…</p>}
      {error && !report && (
        <p role="alert" className="rounded-lg border border-red-900/60 bg-red-950/40 p-3 text-sm text-red-200">
          Could not load delivery latency: {error.message}
        </p>
      )}
      {report && (
        <>
          {(report.eligible_count ?? 0) === 0 && (report.generations ?? []).length === 0 && (
            <p data-testid="delivery-no-data" className="text-sm text-gray-500">
              No data: no completed code generations in this cohort.
            </p>
          )}
          <Headline report={report} />
          <Breakdown report={report} />
          <GenerationTable report={report} history={deploys.data} taskFilter={taskFilter} />
        </>
      )}
    </section>
  );
}
