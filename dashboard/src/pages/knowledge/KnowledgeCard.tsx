import { KnowledgeBadgeRow } from "./KnowledgeBadges";
import { formatKnowledgeTimestamp, type KnowledgeListItemView } from "./model";

export interface KnowledgeCardProps {
  item: KnowledgeListItemView;
  selected?: boolean;
  onSelect: () => void;
}

/**
 * One knowledge record as a selectable card. Same markers as `TaskCard`
 * (`data-listnav`, `data-primary-control`) so keyboard list navigation and
 * the touch-target rule treat it the same; `data-knowledge-row` instead of
 * `data-task-row` so nothing that walks task rows ever picks it up. No
 * claim, complete, retry, push, priority or progress control belongs here.
 */
export default function KnowledgeCard({ item, selected = false, onSelect }: KnowledgeCardProps) {
  return (
    <button
      type="button"
      data-knowledge-row={item.recordId}
      data-listnav="1"
      data-primary-control
      aria-pressed={selected}
      onClick={onSelect}
      className={`block w-full rounded-lg border px-3 py-2 text-left ${
        selected ? "border-indigo-400/60 bg-indigo-500/15" : "border-gray-800 bg-gray-900/60 hover:bg-gray-900"
      }`}
    >
      <span className="line-clamp-2 font-medium text-indigo-300 [overflow-wrap:anywhere]">{item.title}</span>
      {item.summary && (
        <span className="mt-0.5 line-clamp-2 block text-xs text-gray-400 [overflow-wrap:anywhere]">{item.summary}</span>
      )}
      <span className="mt-1 block">
        <KnowledgeBadgeRow item={item} staleReason={item.staleReason} />
      </span>
      <span className="mt-1 flex flex-wrap items-center gap-x-2 text-[10px] text-gray-500">
        <span className="truncate font-mono">{item.alias}</span>
        <span>
          <span className="sr-only">Updated </span>
          {formatKnowledgeTimestamp(item.updatedAt)}
        </span>
        {item.tags.length > 0 && <span className="truncate">{item.tags.map((tag) => `#${tag}`).join(" ")}</span>}
      </span>
    </button>
  );
}
