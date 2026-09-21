import { ArrowUturnUpIcon } from "@heroicons/react/24/outline";

interface Crumb { id: string; title: string }
interface Props { projectName: string; ancestors: Crumb[]; current: Crumb | null; onSelect: (id: string | null) => void }

/**
 * The path into the container the canvas has entered. Containers are never
 * expanded in place, so this is the only way back out: every crumb is a
 * destination, and the leading control goes up exactly one level (the project
 * root when the entered container is a top-level one).
 */
export default function Breadcrumbs({ projectName, ancestors, current, onSelect }: Props) {
  const crumbs: (Crumb | null)[] = [null, ...ancestors];
  const parent = ancestors.length > 0 ? ancestors[ancestors.length - 1]!.id : null;
  return (
    <nav aria-label="Focus path" className="flex shrink-0 flex-wrap items-center gap-1 border-b border-gray-800 px-4 py-1 text-xs text-gray-300">
      <button type="button" aria-label="Up one level"
        title={`Up one level: ${ancestors.length > 0 ? ancestors[ancestors.length - 1]!.title : projectName}`}
        className="mr-1 flex items-center gap-1 rounded border border-gray-700 px-1.5 py-0.5 hover:bg-white/10"
        onClick={() => onSelect(parent)}>
        <ArrowUturnUpIcon aria-hidden className="h-3.5 w-3.5" />Up
      </button>
      {crumbs.map((c, i) => (
        <span key={c?.id ?? "root"} className="flex items-center gap-1">
          {i > 0 && <span aria-hidden className="text-gray-600">›</span>}
          <button type="button" className="rounded px-1 hover:bg-white/10 hover:underline" onClick={() => onSelect(c?.id ?? null)}>{c ? c.title : projectName}</button>
        </span>
      ))}
      {current && <span className="flex items-center gap-1"><span aria-hidden className="text-gray-600">›</span><span aria-current="page" className="px-1 font-medium text-white">{current.title}</span></span>}
    </nav>
  );
}
