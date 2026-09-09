import { useId, useState } from "react";
import { usePoolScale, usePoolStatus, useProfiles } from "../../api/hooks";
import { scaleRequest, validateBounds, type BoundsDraft } from "./PoolScaleFields";
import { isPoolProfile, poolAddress } from "./pools";

const inputClass = "mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";

/**
 * Configure a worker pool without going through an agent row first.
 *
 * A pool is not a row the dashboard creates: the daemon resolves one pool per
 * ``lifecycle: pool`` profile and sizes it fleet-wide, so "create" here means
 * giving that pool its bounds and opening its view. There is nothing to pick
 * but the profile — the project a worker lands in is the placer's decision at
 * launch, not a field on a form — so the bounds are described as what they
 * are: a fleet-wide floor and ceiling, still filtered by each project's own
 * ``max_concurrent_agents`` cap at placement time.
 */
export default function AddPool({ onCreated, onCancel }: {
  onCreated: (poolKey: string) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const scale = usePoolScale();
  const { data: profiles = [] } = useProfiles();
  const { data: pools = [] } = usePoolStatus();

  // A profile with pool lifecycle is eligible even before the daemon has
  // measured a pool for it; pool_status rows cover the reverse case, where the
  // daemon already sizes a pool the profile list has not caught up with.
  const eligible = profiles.filter((profile) =>
    isPoolProfile(profile) || pools.some((pool) => pool.profile_id === profile.id));

  const [profileId, setProfileId] = useState("");
  const [bounds, setBounds] = useState<BoundsDraft>({ min: "1", max: "" });

  // One pool per profile, fleet-wide: an existing row means this save is a
  // reconfiguration, not a second pool.
  const existing = pools.find((pool) => pool.profile_id === profileId);
  const invalid = validateBounds(bounds);
  const set = (key: keyof BoundsDraft, value: string) => setBounds({ ...bounds, [key]: value });

  return (
    <form aria-label="Create agent pool" className="max-h-[65vh] shrink-0 space-y-4 overflow-auto rounded-xl border border-gray-700 bg-gray-900 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (invalid || !profileId) return;
        scale.mutate(scaleRequest(bounds, profileId), {
          onSuccess: () => onCreated(poolAddress(profileId)),
        });
      }}>
      <p className="text-sm text-gray-400">
        Elastic capacity for the whole fleet, not a named worker. The daemon starts and drains
        instances between the bounds below, places each one into a project with a free workspace
        slot, and each instance claims its own tasks — you never add or delete an instance by hand.
      </p>
      <fieldset disabled={scale.isPending} className="grid gap-4 sm:grid-cols-2">
        <label className="text-xs text-gray-400" htmlFor={id + "-profile"}>
          Pool profile
          <select id={id + "-profile"} aria-label="Pool profile" required value={profileId} className={inputClass}
            onChange={(event) => {
              setProfileId(event.target.value);
              const pool = pools.find((row) => row.profile_id === event.target.value);
              if (pool) setBounds({ min: String(pool.min_active), max: pool.max_active == null ? "" : String(pool.max_active) });
            }}>
            <option value="">Choose a pool profile</option>
            {eligible.map((profile) => (
              <option key={profile.id} value={profile.id}>{profile.name || profile.id}</option>
            ))}
          </select>
          <span className="mt-1 block text-[10px] text-gray-500">
            Only <span className="text-sky-300">lifecycle: pool</span> profiles can back a pool.
          </span>
        </label>
        <label className="text-xs text-gray-400" htmlFor={id + "-min"}>
          Minimum active workers
          <input id={id + "-min"} type="number" min={0} inputMode="numeric" value={bounds.min}
            onChange={(event) => set("min", event.target.value)} className={inputClass} />
        </label>
        <label className="text-xs text-gray-400" htmlFor={id + "-max"}>
          Maximum active workers
          <input id={id + "-max"} type="number" min={1} inputMode="numeric" value={bounds.max}
            placeholder="Unbounded" onChange={(event) => set("max", event.target.value)} className={inputClass} />
        </label>
      </fieldset>
      <p className="text-xs text-gray-500">
        Bounds are saved on the system profile in the vault and count every worker on this
        profile, across every project; each project&apos;s own concurrency cap still limits how
        many land there. Leave the maximum empty for no upper bound.
      </p>
      {eligible.length === 0 && (
        <p role="alert" className="text-sm text-amber-300">
          No profile runs as a pool yet. Give one pool lifecycle first — <code>aq pool
          set-lifecycle &lt;profile&gt; pool</code> — then it becomes available here.
        </p>
      )}
      {existing && (
        <p role="status" className="text-xs text-gray-400">
          {profileId} already runs a pool ({existing.desired} desired, {existing.running_busy} busy
          across {existing.projects?.length ?? 0} project{(existing.projects?.length ?? 0) === 1 ? "" : "s"}).
          Saving updates its bounds rather than adding a second pool.
        </p>
      )}
      {invalid && <p role="alert" className="text-xs text-amber-300">{invalid}</p>}
      {scale.error && <p role="alert" className="text-sm text-red-300">{scale.error.message}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={scale.isPending || !profileId || !!invalid}
          className="rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-40">
          {scale.isPending ? "Creating…" : "Create agent pool"}
        </button>
        <button type="button" disabled={scale.isPending} onClick={onCancel}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800">Cancel</button>
      </div>
    </form>
  );
}
