import { KNOWLEDGE_ACTION_LABELS, type KnowledgeAction } from "../../pages/knowledge/model";

export interface KnowledgeActionsProps {
  /** Server-returned; nothing else makes a button appear. */
  allowed: KnowledgeAction[];
  onEdit: () => void;
  onHistory: () => void;
  /** Link, retire, restore, create task and propose correction are announced to the owner. */
  onAction: (action: KnowledgeAction) => void;
  /** Set while a historical revision is shown: editing starts from the current one. */
  editDisabledReason?: string | null;
}

const ORDER: KnowledgeAction[] = [
  "edit", "propose_correction", "history", "link", "retire", "restore", "create_task",
];

const BUTTON = "rounded border border-gray-700 px-2 py-1 text-xs text-gray-200 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50";

/**
 * The knowledge toolbar. Only the actions the server allowed render, in a
 * fixed order; no claim, complete, retry, push, priority or allocation
 * control ever appears here — those stay in `TaskRowActions`.
 */
export default function KnowledgeActions({ allowed, onEdit, onHistory, onAction, editDisabledReason = null }: KnowledgeActionsProps) {
  const visible = ORDER.filter((action) => allowed.includes(action));
  if (visible.length === 0) return null;
  return (
    <div role="toolbar" aria-label="Knowledge actions" className="flex flex-wrap gap-1.5">
      {visible.map((action) => {
        if (action === "edit") {
          return (
            <button key={action} type="button" className={BUTTON} onClick={onEdit}
              disabled={editDisabledReason !== null} title={editDisabledReason ?? undefined}>
              {KNOWLEDGE_ACTION_LABELS[action]}
            </button>
          );
        }
        if (action === "history") {
          return (
            <button key={action} type="button" className={BUTTON} onClick={onHistory}>
              {KNOWLEDGE_ACTION_LABELS[action]}
            </button>
          );
        }
        return (
          <button key={action} type="button" className={BUTTON} onClick={() => onAction(action)}>
            {KNOWLEDGE_ACTION_LABELS[action]}
          </button>
        );
      })}
    </div>
  );
}
