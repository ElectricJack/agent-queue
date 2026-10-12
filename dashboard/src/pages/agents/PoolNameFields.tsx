import { useId, useState } from "react";
import { usePoolRename, type PoolStatusRow } from "../../api/hooks";
import { poolDisplayName } from "./pools";

export default function PoolNameFields({ pool }: { pool: PoolStatusRow }) {
  const id = useId();
  const rename = usePoolRename();
  const [draft, setDraft] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const baseline = poolDisplayName(pool);
  const value = draft ?? baseline;
  const name = value.trim();
  const dirty = name !== baseline;
  const invalid = !name ? "Enter a display name."
    : Array.from(name).length > 120 ? "Use 120 characters or fewer."
    : /[\p{Cc}]/u.test(value) ? "Use a name without control characters." : null;

  return (
    <form aria-label="Pool name" className="space-y-2" onSubmit={(event) => {
      event.preventDefault();
      if (!dirty || invalid || rename.isPending) return;
      rename.mutate({ profile_id: pool.profile_id, name }, {
        onSuccess: () => { setDraft(null); setSaved(true); },
      });
    }}>
      <label htmlFor={id} className="block text-xs text-gray-400">
        Display name
        <input id={id} value={value} required disabled={rename.isPending}
          onChange={(event) => { setDraft(event.target.value); setSaved(false); }}
          className="mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none" />
      </label>
      <p className="text-xs text-gray-500">Profile ID: <code>{pool.profile_id}</code></p>
      {dirty && invalid && <p role="alert" className="text-xs text-amber-300">{invalid}</p>}
      {rename.error && <p role="alert" className="text-sm text-red-300">{rename.error.message}</p>}
      {saved && <p role="status" className="text-xs text-emerald-400">Pool name saved.</p>}
      <button type="submit" disabled={!dirty || !!invalid || rename.isPending}
        className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
        {rename.isPending ? "Saving…" : "Save pool name"}
      </button>
      <button type="button" disabled={draft === null || rename.isPending}
        onClick={() => { setDraft(null); setSaved(false); rename.reset(); }}
        className="ml-2 rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800 disabled:opacity-40">
        Discard name changes
      </button>
    </form>
  );
}
