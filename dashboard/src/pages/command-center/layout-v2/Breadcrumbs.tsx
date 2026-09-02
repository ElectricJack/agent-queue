interface Crumb { id: string; title: string }
interface Props { projectName: string; ancestors: Crumb[]; current: Crumb | null; onSelect: (id: string | null) => void }

export default function Breadcrumbs({ projectName, ancestors, current, onSelect }: Props) {
  const crumbs: (Crumb | null)[] = [null, ...ancestors];
  return (
    <nav aria-label="Focus path" className="flex shrink-0 flex-wrap items-center gap-1 border-b border-gray-800 px-4 py-1 text-xs text-gray-300">
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
