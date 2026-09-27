import { Link, useLocation } from "react-router-dom";
import { CheckCircleIcon, ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import type { ProviderAllocationApplyResponse } from "../../api/client";
import { applyOutcome, eventsSearch, plural, profileStatusText } from "./allocation";

/**
 * Step 3: what apply did, row by row.
 *
 * Only ``status: applied`` reads as success.  A partial or rolled-back apply
 * is an alert that lists every profile row (applied, failed, rolled back,
 * rollback failed, not reached) and every session action with its error, so
 * the operator sees what landed and what did not; the spec forbids
 * summarizing it as success.  The request id links to the Events drawer,
 * filtered to the ``pool.*`` events that carry it.
 */
export default function AllocationResult({ result, onDone }: {
  result: ProviderAllocationApplyResponse;
  onDone: () => void;
}) {
  const location = useLocation();
  const outcome = applyOutcome(result);
  const profiles = result.profiles ?? [];
  const actions = result.session_actions ?? [];
  const warnings = result.warnings ?? [];
  const placement = result.preference?.placement;
  const placementHeld = placement?.held?.length ?? 0;
  const placementErrors = placement?.errors ?? [];

  return (
    <div className="space-y-4">
      {outcome === "applied" ? (
        <div role="status" className="flex items-start gap-2 rounded border border-emerald-800 bg-emerald-950/30 p-3 text-sm text-emerald-200">
          <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <h3 className="font-semibold">Allocation applied</h3>
            <p className="text-xs text-emerald-300/80">Every previewed change landed.</p>
          </div>
        </div>
      ) : (
        <div role="alert" className="flex items-start gap-2 rounded border border-red-800 bg-red-950/40 p-3 text-sm text-red-200">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-1">
            <h3 className="font-semibold">
              {outcome === "rolled_back" ? "Allocation rolled back" : "Allocation partially applied"}
            </h3>
            <p className="text-xs">
              {outcome === "rolled_back"
                ? "A change failed and the earlier edits were undone. Review the rows below before trying again."
                : "Some changes landed and others did not. Review each row below; this is not a success."}
            </p>
            {result.error && <p className="font-mono text-xs text-red-300">{result.error}</p>}
          </div>
        </div>
      )}

      {result.request_id && (
        <p className="text-xs text-gray-400">
          Request{" "}
          {/* The Events drawer opens beside the page, under this overlay: close it on the way. */}
          <Link to={{ pathname: location.pathname, search: eventsSearch(location.search, result.request_id) }}
            onClick={onDone} className="font-mono text-indigo-300 underline">
            {"Events for " + result.request_id}
          </Link>
          {result.actor ? " · by " + result.actor : ""}
        </p>
      )}

      {profiles.length > 0 && (
        <ul aria-label="Profile results" className="divide-y divide-gray-800 rounded border border-gray-800 text-xs">
          {profiles.map((row) => (
            <li key={row.profile_id} className="flex flex-col gap-0.5 px-3 py-1.5">
              <span className="flex items-center justify-between gap-2">
                <span className="font-mono text-gray-200">{row.profile_id}</span>
                <span className={row.status === "applied" ? "text-emerald-300" : row.status === "skipped" ? "text-gray-400" : "text-red-300"}>
                  {profileStatusText(row.status)}
                </span>
              </span>
              {row.error && <span className="text-red-300">{row.error}</span>}
              {row.compensation_error && <span className="text-red-300">{"compensation failed: " + row.compensation_error}</span>}
            </li>
          ))}
        </ul>
      )}

      {actions.length > 0 && (
        <ul aria-label="Session actions" className="space-y-0.5 text-xs text-gray-400">
          {actions.map((action, index) => (
            <li key={action.session_id + ":" + action.action + ":" + index}>
              <span className="font-mono">{action.session_id}</span>
              {` · ${action.action}${action.reason ? " (" + action.reason + ")" : ""}`}
              {action.error && <span className="text-red-300">{" — " + action.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {placement && (
        <p className="text-xs text-gray-400">
          {`Re-placed ${plural(placement.moved?.length ?? 0, "queued task")} onto this provider`}
          {placementHeld > 0 ? `; ${placementHeld} held` : ""}
          {placementErrors.length > 0 && <span className="block text-red-300">{placementErrors.join("; ")}</span>}
        </p>
      )}

      {warnings.length > 0 && (
        <ul className="space-y-0.5 text-xs text-amber-300">
          {warnings.map((warning) => <li key={warning.code}>{warning.message}</li>)}
        </ul>
      )}

      <div className="flex justify-end">
        <button type="button" onClick={onDone}
          className="rounded border border-gray-700 px-3 py-2 text-sm text-gray-300 hover:bg-gray-800">
          Done
        </button>
      </div>
    </div>
  );
}
