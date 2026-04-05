import { useState } from "react";
import { Link } from "react-router-dom";
import { useTasks, useProjects } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";

export default function Tasks() {
  const { data: projects } = useProjects();
  const [activeTab, setActiveTab] = useState<string>("");
  const [showCompleted, setShowCompleted] = useState(
    () => localStorage.getItem("tasks:showCompleted") === "true",
  );
  const toggleShowCompleted = (v: boolean) => {
    setShowCompleted(v);
    localStorage.setItem("tasks:showCompleted", String(v));
  };

  const projectList = projects ?? [];
  const selectedProjectId = activeTab || projectList[0]?.id || "";

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Tasks</h1>

      {/* Tab bar with toggle on the right */}
      {projectList.length > 0 && (
        <div className="flex items-center justify-between border-b border-gray-800">
          <div className="flex gap-1">
            {projectList.map((p) => (
              <button
                key={p.id}
                onClick={() => setActiveTab(p.id)}
                className={`px-4 py-2 text-sm font-medium transition-colors ${
                  selectedProjectId === p.id
                    ? "border-b-2 border-indigo-400 text-indigo-400"
                    : "text-gray-400 hover:text-gray-200"
                }`}
              >
                {p.name || p.id}
              </button>
            ))}
          </div>

          {/* Toggle switch */}
          <button
            role="switch"
            aria-checked={showCompleted}
            onClick={() => toggleShowCompleted(!showCompleted)}
            className="flex items-center gap-2 pb-1 text-sm text-gray-400"
          >
            <span>Show completed</span>
            <span
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ${
                showCompleted ? "bg-indigo-500" : "bg-gray-700"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 translate-y-0.5 rounded-full bg-white shadow ring-0 transition-transform duration-200 ${
                  showCompleted ? "translate-x-4.5" : "translate-x-0.5"
                }`}
              />
            </span>
          </button>
        </div>
      )}

      {selectedProjectId ? (
        <TaskTable projectId={selectedProjectId} showCompleted={showCompleted} />
      ) : (
        <p className="text-sm text-gray-500">No projects found.</p>
      )}
    </div>
  );
}

function TaskTable({ projectId, showCompleted }: { projectId: string; showCompleted: boolean }) {
  const { data: tasks, isLoading } = useTasks(projectId, { showAll: showCompleted });

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!tasks?.length) return <p className="text-sm text-gray-500">No tasks in this project.</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">Title</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Priority</th>
            <th className="px-4 py-3">Agent</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {tasks.map((task) => (
            <tr key={task.id} className="hover:bg-gray-900/50">
              <td className="max-w-sm px-4 py-3">
                <Link
                  to={`/tasks/${task.id}`}
                  className="font-medium text-indigo-400 hover:underline"
                >
                  {task.title}
                </Link>
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={task.status} />
              </td>
              <td className="px-4 py-3 text-gray-400">
                {task.priority != null ? `P${task.priority}` : "-"}
              </td>
              <td className="px-4 py-3 text-gray-400">{task.agent_name ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
