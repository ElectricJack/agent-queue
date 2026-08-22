import type { MergedGraph } from "./types";

interface Project {
  id: string;
  name: string;
}

interface Props {
  projects: Project[];
  graph: MergedGraph;
  selected: string[];
  onToggle: (pid: string) => void;
}

function vitals(pid: string, g: MergedGraph) {
  const tasks = g.tasks.filter((t) => g.taskProject[t.id] === pid);
  const running = tasks.filter((t) => t.status === "IN_PROGRESS").length;
  const blocked = tasks.filter((t) => t.is_blocked).length;
  const ready = tasks.filter((t) => t.status === "READY" && !t.is_blocked).length;
  const openGates = g.gates.filter(
    (gt) =>
      gt.status === "open" &&
      (gt.task_ids ?? []).some((tid) => g.taskProject[tid] === pid),
  ).length;
  return { running, blocked, ready, openGates };
}

export default function ProjectStrip({ projects, graph, selected, onToggle }: Props) {
  return (
    <div className="flex gap-2 overflow-x-auto border-b border-gray-800 bg-gray-950 p-2">
      {projects.map((p) => {
        const v = vitals(p.id, graph);
        const on = selected.includes(p.id);
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => onToggle(p.id)}
            className={`min-w-[180px] rounded border p-2 text-left text-xs ${on ? "border-indigo-500 bg-indigo-950" : "border-gray-800 bg-gray-900 hover:bg-gray-800"}`}
          >
            <div className="mb-1 truncate font-semibold text-gray-100">{p.name}</div>
            <div className="grid grid-cols-4 gap-1 text-center">
              <span title="running" className="rounded bg-indigo-500/20 py-0.5">
                {v.running}
              </span>
              <span title="ready" className="rounded bg-sky-500/20 py-0.5">
                {v.ready}
              </span>
              <span title="blocked" className="rounded bg-amber-500/20 py-0.5">
                {v.blocked}
              </span>
              <span title="open gates" className="rounded bg-gray-500/20 py-0.5">
                {v.openGates}
              </span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
