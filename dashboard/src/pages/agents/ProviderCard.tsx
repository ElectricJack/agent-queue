import { Link, useLocation } from "react-router-dom";
import { AdjustmentsHorizontalIcon } from "@heroicons/react/24/outline";
import type { ProviderAllocationGroup, ProviderAllocationProject } from "../../api/client";
import { TONE_CLASSES, stateLabel, stateTone } from "../metrics/providerAvailabilityFormat";
import {
  ceilingText,
  eventsSearch,
  liveCount,
  plural,
  preferenceText,
  profileBounds,
  providerLabel,
} from "./allocation";

/**
 * One provider: the configured ceiling over its pool profiles, the supply on
 * them, every ordinary worker profile with its own bounds, the projects and
 * their routing preference, and badges for what bulk allocation leaves alone.
 *
 * Read-only; "Edit allocation" opens the preview/apply drawer.  Surgical,
 * one-pool changes stay in each pool's own view.
 */
export default function ProviderCard({ group, projects, onEdit }: {
  group: ProviderAllocationGroup;
  projects: ProviderAllocationProject[];
  onEdit: () => void;
}) {
  const location = useLocation();
  const label = providerLabel(group.provider, group.vendor);
  const state = group.state ?? "available";
  const supply = group.supply;
  const profiles = group.profiles ?? [];
  const manual = group.manual_agents ?? [];
  const pinned = group.pinned_tasks ?? 0;
  const last = group.last_allocation;
  const liveIn = (projectId: string) => profiles.reduce((sum, profile) => {
    const row = (profile.projects ?? []).find((item) => item.project_id === projectId);
    return sum + (row ? liveCount(row) : 0);
  }, 0);

  return (
    <section aria-label={label + " provider"} className="flex flex-col gap-3 rounded-xl border border-gray-800 p-3">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-100">
            {label}
            <span className={"rounded border px-1.5 py-0.5 text-[10px] font-normal " + TONE_CLASSES[stateTone(state)].pill}>
              {stateLabel(state)}
            </span>
          </h3>
          <p className="mt-0.5 text-xs text-gray-400" title="The sum of this provider's pool-profile bounds. Bounds stay per profile; there is no provider-wide cap.">
            {"Pool ceiling " + ceilingText(group.ceiling)}
          </p>
        </div>
        <button type="button" aria-label={"Edit " + label + " allocation"} onClick={onEdit}
          className="flex shrink-0 items-center gap-1.5 rounded border border-gray-700 px-2.5 py-1.5 text-xs text-gray-300 hover:bg-gray-800">
          <AdjustmentsHorizontalIcon className="h-3.5 w-3.5" />Edit allocation
        </button>
      </header>

      <p aria-label={label + " supply"} className="flex flex-wrap gap-x-2 gap-y-0.5 font-mono text-[10px] text-gray-500">
        <span className="text-gray-300">live {liveCount(supply)}</span>
        <span>idle {supply.idle ?? 0}</span>
        <span className={(supply.busy ?? 0) > 0 ? "text-emerald-400" : undefined}>busy {supply.busy ?? 0}</span>
        <span>starting {supply.starting ?? 0}</span>
        <span>draining {supply.draining ?? 0}</span>
        {(supply.unresponsive ?? 0) > 0 && <span className="text-amber-300">unresponsive {supply.unresponsive}</span>}
        {supply.ready != null && <span>ready {supply.ready}</span>}
      </p>

      <div className="flex flex-wrap gap-1.5 text-[10px]">
        {pinned > 0 && (
          <span title="Tasks explicitly pinned to one of these profiles. Bulk allocation never rewrites a pin."
            className="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-300">
            {plural(pinned, "pinned task")}
          </span>
        )}
        {manual.length > 0 && (
          <span title={"Manual agent definitions on this provider: " + manual.map((agent) => agent.name).join(", ")
            + ". Bulk allocation never edits, disables or retires them."}
            className="rounded bg-sky-500/10 px-1.5 py-0.5 text-sky-300">
            {plural(manual.length, "manual agent")}
          </span>
        )}
      </div>

      <ul aria-label={label + " profiles"} className="divide-y divide-gray-800/70 rounded border border-gray-800/70">
        {profiles.map((profile) => (
          <li key={profile.profile_id} data-profile-id={profile.profile_id} className="flex flex-col gap-0.5 px-2 py-1.5">
            <span className="flex items-center justify-between gap-2">
              <span className="truncate text-xs text-gray-200">{profile.profile_id}</span>
              <span className="flex shrink-0 items-center gap-1.5 text-[10px]">
                {profile.enabled === false && <span className="text-amber-300">disabled</span>}
                <span className={"rounded px-1.5 py-0.5 " + (profile.lifecycle === "pool" ? "bg-sky-500/10 text-sky-300" : "bg-gray-800 text-gray-400")}>
                  {profile.lifecycle === "pool" ? "Pool" : "Task"}
                </span>
              </span>
            </span>
            <span className="flex flex-wrap gap-x-2 font-mono text-[10px] text-gray-500">
              <span>{profile.intelligence_class || "no class"}</span>
              {profile.lifecycle === "pool" && (
                <span title={"min_active " + (profile.min_active ?? 0) + ", max_active " + (profile.max_active ?? "unbounded")}>
                  {profileBounds(profile)}
                </span>
              )}
              <span>live {liveCount(profile.supply)}</span>
              {(profile.pinned?.count ?? 0) > 0 && <span className="text-amber-300">{profile.pinned.count} pinned</span>}
            </span>
          </li>
        ))}
        {profiles.length === 0 && <li className="px-2 py-1.5 text-xs text-gray-500">No ordinary worker profiles.</li>}
      </ul>

      {projects.length > 0 && (
        <ul aria-label={label + " projects"} className="space-y-0.5 text-[11px] text-gray-400">
          {projects.map((project) => (
            <li key={project.project_id} className="flex items-center justify-between gap-2">
              <span className="truncate font-mono">{project.project_id}</span>
              <span className="shrink-0 text-gray-500">
                {liveIn(project.project_id)} live · {preferenceText(project.preferred_provider, group.provider)}
              </span>
            </li>
          ))}
        </ul>
      )}

      {last?.request_id && (
        <p className="text-[10px] text-gray-500">
          Last allocation {last.status ?? "recorded"}{last.actor ? " by " + last.actor : ""}:{" "}
          <Link to={{ pathname: location.pathname, search: eventsSearch(location.search, last.request_id) }}
            className="font-mono text-indigo-300 underline">
            {last.request_id}
          </Link>
        </p>
      )}
    </section>
  );
}
