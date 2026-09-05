import { useId, useState } from "react";
import { usePoolScale, type PoolStatusRow } from "../../api/hooks";

export interface BoundsDraft { min: string; max: string }

const inputClass = "mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";

export function boundsOf(pool: PoolStatusRow): BoundsDraft {
  return { min: String(pool.min_active), max: pool.max_active == null ? "" : String(pool.max_active) };
}

/**
 * Bounds validation, matching what ``_cmd_pool_scale`` will accept.
 *
 * An empty max means "no upper bound". The typed API represents that as an
 * explicit ``max: null``; omitting the key would leave the current bound
 * unchanged instead.
 */
export function validateBounds(draft: BoundsDraft): string | null {
  const min = draft.min.trim();
  const max = draft.max.trim();
  if (!/^-?\d+$/.test(min)) return "Min must be a whole number of workers.";
  if (Number(min) < 0) return "Min must be 0 or more.";
  if (max === "") return null;
  if (!/^-?\d+$/.test(max)) return "Max must be a whole number of workers, or empty for unbounded.";
  if (Number(max) < 1) return "Max must be 1 or more.";
  if (Number(max) < Number(min)) return "Max must be greater than or equal to min.";
  return null;
}

/** Turn a validated draft into a ``pool_scale`` body; null clears the max. */
export function scaleRequest(draft: BoundsDraft, profileId: string) {
  const max = draft.max.trim();
  return {
    profile_id: profileId,
    min: Number(draft.min.trim()),
    max: max === "" ? null : Number(max),
  };
}

/**
 * Edit one pool profile's ``min_active`` / ``max_active``.
 *
 * ``pool_scale`` writes the bounds into the **system** profile's vault
 * markdown, so a save survives the next vault sync — unlike a direct
 * profile-row edit. Profiles are global, so the bounds apply to every
 * project's pool for this profile; each project's own concurrency cap still
 * limits it at runtime.
 */
export default function PoolScaleFields({ pool }: { pool: PoolStatusRow }) {
  const id = useId();
  const scale = usePoolScale();
  const [draft, setDraft] = useState<BoundsDraft | null>(null);
  const [saved, setSaved] = useState(false);
  // Polling refreshes bounds while the user types; only a pristine form follows.
  const baseline = boundsOf(pool);
  const form = draft ?? baseline;
  const dirty = form.min !== baseline.min || form.max !== baseline.max;
  const invalid = validateBounds(form);
  const set = (key: keyof BoundsDraft, value: string) => {
    setDraft({ ...form, [key]: value });
    setSaved(false);
  };

  return (
    <div aria-label={pool.profile_id + " pool bounds"} className="space-y-3">
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="text-xs text-gray-400" htmlFor={id + "-min"}>
          Minimum active workers
          <input id={id + "-min"} type="number" min={0} inputMode="numeric" value={form.min}
            onChange={(event) => set("min", event.target.value)} className={inputClass} />
        </label>
        <label className="text-xs text-gray-400" htmlFor={id + "-max"}>
          Maximum active workers
          <input id={id + "-max"} type="number" min={1} inputMode="numeric" value={form.max}
            placeholder="Unbounded" onChange={(event) => set("max", event.target.value)} className={inputClass} />
        </label>
      </div>
      <p className="text-xs text-gray-500">
        The daemon keeps at least the minimum alive and never exceeds the maximum; leave the
        maximum empty for no upper bound. Bounds are saved on the system profile in the vault
        and apply to every project that runs this pool.
      </p>
      {invalid && dirty && <p role="alert" className="text-xs text-amber-300">{invalid}</p>}
      {scale.error && <p role="alert" className="text-sm text-red-300">{scale.error.message}</p>}
      {saved && <p role="status" className="text-xs text-emerald-400">Pool bounds saved.</p>}
      <div className="flex items-center gap-2">
        <button type="button" disabled={!dirty || !!invalid || scale.isPending}
          onClick={() => scale.mutate(scaleRequest(form, pool.profile_id), {
            onSuccess: () => { setDraft(null); setSaved(true); },
          })}
          className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
          {scale.isPending ? "Saving…" : "Save pool bounds"}
        </button>
        <button type="button" disabled={!dirty || scale.isPending}
          onClick={() => { setDraft(null); setSaved(false); scale.reset(); }}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800 disabled:opacity-40">
          Discard changes
        </button>
      </div>
    </div>
  );
}
