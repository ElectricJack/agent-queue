import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import StatusBadge from "./StatusBadge";
import type { Task } from "../api/hooks";
import { rememberTaskPreview } from "../panes/task-detail/preview";

export interface TaskCardProps {
  task: Task;
  projectName?: string;
  /** A note under the title, e.g. why a held task waits. */
  note?: ReactNode;
  selected?: boolean;
  /** A link (the focus list)… */
  to?: string;
  /** …or a selection (the Tasks tab's pane, below 768 px). */
  onSelect?: () => void;
}

/**
 * One task as a touch card (mobile dashboard §3 gap 6): the title wraps, even
 * an unbroken Unicode one, and the whole card is the target. It keeps the
 * table row's `data-task-row` and `data-listnav` markers, so the perf harness
 * and keyboard list navigation (useListNav) find it the same way.
 */
export default function TaskCard({ task, projectName, note, selected = false, to, onSelect }: TaskCardProps) {
  const className = `block w-full rounded-lg border px-3 py-2 text-left ${
    selected ? "border-indigo-400/60 bg-indigo-500/15" : "border-gray-800 bg-gray-900/60 hover:bg-gray-900"
  }`;
  const body = (
    <>
      <span className="line-clamp-2 font-medium text-indigo-300 [overflow-wrap:anywhere]">{task.title || task.id}</span>
      <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-400">
        <StatusBadge status={task.status} />
        {task.priority != null && <span>P{task.priority}</span>}
        <span className="min-w-0 truncate">{task.assigned_agent || "Unassigned"}</span>
        {projectName && <span className="min-w-0 truncate">{projectName}</span>}
      </span>
      <span className="mt-0.5 block truncate font-mono text-[10px] text-gray-500">{task.id}</span>
      {note}
    </>
  );
  if (to) {
    return (
      <Link to={to} data-task-row={task.id} data-listnav="1" data-primary-control className={className} onClick={() => rememberTaskPreview(task)}>
        {body}
      </Link>
    );
  }
  return (
    <button type="button" data-task-row={task.id} data-listnav="1" data-primary-control aria-pressed={selected} className={className} onClick={onSelect}>
      {body}
    </button>
  );
}
