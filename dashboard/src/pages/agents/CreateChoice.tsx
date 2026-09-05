import { UserPlusIcon, RectangleStackIcon } from "@heroicons/react/24/outline";
import type { CreateMode } from "./useAgentSelection";

/**
 * The fork every creation starts at.
 *
 * A durable agent and a worker pool are different objects — different scope
 * (global vs per project), different lifecycle (one long-lived session vs
 * capacity the daemon sizes), different management surface. Presenting one
 * "Add agent" form for both let an operator pick a ``lifecycle: pool``
 * profile and get pool capacity that the flock then files under its pool
 * entry, which read as a failed creation.
 */
const choices: { mode: Exclude<CreateMode, "choice">; label: string; scope: string; body: string; Icon: typeof UserPlusIcon }[] = [
  {
    mode: "agent",
    label: "Create agent",
    scope: "One durable worker · global",
    body: "A single named worker, shared across every project. It sits idle until the "
      + "scheduler assigns it a task, and you start, configure and delete it by hand.",
    Icon: UserPlusIcon,
  },
  {
    mode: "pool",
    label: "Create agent pool",
    scope: "Elastic capacity · per project",
    body: "Capacity for one project, not a named worker. The daemon starts and drains "
      + "instances between the bounds you set, and each instance claims its own work.",
    Icon: RectangleStackIcon,
  },
];

export default function CreateChoice({ onChoose, onCancel }: {
  onChoose: (mode: Exclude<CreateMode, "choice">) => void;
  onCancel: () => void;
}) {
  return (
    <section aria-label="Create agent or pool"
      className="shrink-0 space-y-4 rounded-xl border border-gray-700 bg-gray-900 p-4">
      <p className="text-sm text-gray-400">What do you want to create?</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {choices.map(({ mode, label, scope, body, Icon }) => (
          <button key={mode} type="button" aria-label={label} onClick={() => onChoose(mode)}
            className="flex flex-col gap-1.5 rounded-lg border border-gray-700 bg-gray-950 p-3 text-left hover:border-indigo-500 hover:bg-gray-900">
            <span className="flex items-center gap-2 text-sm font-medium text-gray-100">
              <Icon className="h-4 w-4 text-indigo-300" />{label}
            </span>
            <span className="text-[10px] uppercase tracking-wide text-indigo-300/80">{scope}</span>
            <span className="text-xs leading-relaxed text-gray-400">{body}</span>
          </button>
        ))}
      </div>
      <button type="button" onClick={onCancel}
        className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800">Cancel</button>
    </section>
  );
}
