import { useIntelligenceClasses } from "../../api/hooks";
import { describeProviderMapping, groupIntelligenceClasses } from "../intelligence-classes/mapping";

interface Props {
  value: string;
  onChange: (next: string) => void;
}

export default function IntelligenceClassPicker({ value, onChange }: Props) {
  const { data, isLoading, error } = useIntelligenceClasses();
  const classes = data?.classes ?? [];
  const selected = classes.find((row) => row.id === value);

  if (isLoading) return <p className="text-xs text-gray-500">Loading intelligence classes…</p>;
  if (error) return <p className="text-xs text-red-400">Failed to load: {(error as Error).message}</p>;

  return (
    <div className="space-y-2">
      <select value={value ?? ""} onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none">
        <option value="">— inherit / none —</option>
        {groupIntelligenceClasses(classes).map(({ label, rows }) => (
          <optgroup key={label} label={label}>
            {rows.map((row) => <option key={row.id} value={row.id}>{row.id}</option>)}
          </optgroup>
        ))}
      </select>
      {selected && (
        <div className="rounded-md border border-gray-800 bg-gray-950/60 p-2 text-[11px] text-gray-500">
          <p className="mb-1 text-gray-400">{selected.description}</p>
          <ul className="space-y-0.5 font-mono">
            {Object.entries(selected.mapping).sort(([a], [b]) => a.localeCompare(b)).map(([provider, slice]) => (
              <li key={provider}><span className="text-gray-400">{provider}:</span> {describeProviderMapping(slice)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
