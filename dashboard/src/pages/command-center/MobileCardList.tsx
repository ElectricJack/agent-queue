import { useState } from "react";
import { PlaybookCard } from "./PlaybookNode";
import { TaskCard } from "./TaskNode";
import { useGraphHierarchy } from "./useGraphHierarchy";
import type { GraphViewProps } from "./types";

export default function MobileCardList(props: GraphViewProps) {
  const { graph, onTaskClick, onBackgroundClick, selectedTaskId, playbooks = [], selectedPlaybookId, onPlaybookClick } = props;
  const { projection, toggleExpanded } = useGraphHierarchy(props);
  const [localSelectedId, setLocalSelectedId] = useState<string | null>(null);
  const selectedId = selectedTaskId === undefined ? localSelectedId : selectedTaskId;
  function openTask(id: string) {
    setLocalSelectedId(id);
    onTaskClick(id);
  }
  function clearSelection() {
    setLocalSelectedId(null);
    onBackgroundClick?.();
  }

  return (
    <div
      role="region"
      aria-label="Task list"
      tabIndex={0}
      className="h-full overflow-y-auto space-y-3 p-3 outline-none"
      onClick={(event) => {
        if (!(event.target as HTMLElement).closest("[data-task-card], [data-playbook-card], button, a")) clearSelection();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          clearSelection();
        }
      }}
    >
      {playbooks.map(playbook => <PlaybookCard key={playbook.id} fluid
        selected={selectedPlaybookId === playbook.id}
        data={{ playbook, onOpenPlaybook: onPlaybookClick }} />)}
      {playbooks.length === 0 && projection.tasks.length === 0 && <p className="py-6 text-center text-sm text-gray-500">No tasks or playbooks match these filters.</p>}
      {projection.tasks.map((task) => {
        const hierarchy = projection.details.get(task.id)!;
        const dependencies = projection.edges.filter((edge) => edge.from === task.id && edge.dep_type !== "parent-child");
        return (
          <section key={task.id} style={{ marginLeft: Math.min(hierarchy.depth, 3) * 12 }}>
            <TaskCard
              fluid
              selected={selectedId === task.id}
              data={{
                task, hierarchy,
                gates: graph.gates.filter((gate) => gate.task_ids?.includes(task.id)),
                projectId: graph.taskProject[task.id] ?? "",
                onOpenTask: openTask, onToggleChildren: toggleExpanded,
              }}
            />
            {dependencies.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-[10px] text-gray-400">
                {dependencies.map((edge) => (
                  <li key={JSON.stringify([edge.to, edge.dep_type])}>
                    <button type="button" className="text-left hover:text-indigo-300" onClick={() => openTask(edge.to)}>
                      {edge.dep_type} ← {projection.tasks.find((candidate) => candidate.id === edge.to)?.title ?? edge.to}
                      {edge.count > 1 ? ` ×${edge.count}` : ""}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}
