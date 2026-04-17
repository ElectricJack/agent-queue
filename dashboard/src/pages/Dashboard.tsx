import {
  BoltIcon,
  CpuChipIcon,
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import { useAllAgents, useActiveTasksAllProjects, useHealth, useProjects } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";

export default function Dashboard() {
  const health = useHealth();
  const projects = useProjects();
  const projectIds = (projects.data ?? []).map((p) => p.id);
  const agents = useAllAgents(projectIds);
  const activeTasks = useActiveTasksAllProjects();

  const agentList = agents.data ?? [];
  const taskList = activeTasks.data ?? [];
  const busyAgents = agentList.filter((a) => a.state === "busy").length;
  const failedTasks = taskList.filter((t) => t.status === "failed").length;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Dashboard</h1>

      {/* Stats row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={<BoltIcon className="h-5 w-5 text-green-400" />}
          label="System"
          value={health.data?.status ?? "..."}
        />
        <StatCard
          icon={<CpuChipIcon className="h-5 w-5 text-indigo-400" />}
          label="Agents"
          value={`${busyAgents} / ${agentList.length} busy`}
        />
        <StatCard
          icon={<ClipboardDocumentListIcon className="h-5 w-5 text-blue-400" />}
          label="Active Tasks"
          value={String(taskList.length)}
        />
        <StatCard
          icon={<ExclamationTriangleIcon className="h-5 w-5 text-red-400" />}
          label="Failed"
          value={String(failedTasks)}
        />
      </div>

      {/* Active agents */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Agents</h2>
        {agentList.length === 0 ? (
          <p className="text-sm text-gray-500">No agents registered.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {agentList.map((agent) => (
              <div
                key={agent.workspace_id}
                className="rounded-lg border border-gray-800 bg-gray-900 p-4"
              >
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-medium">{agent.name || agent.workspace_id}</span>
                  <StatusBadge status={agent.state} />
                </div>
                {agent.current_task_title && (
                  <p className="truncate text-sm text-gray-400">
                    {agent.current_task_title}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Active tasks */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Active Tasks</h2>
        {taskList.length === 0 ? (
          <p className="text-sm text-gray-500">No active tasks.</p>
        ) : (
          <div className="space-y-2">
            {taskList.slice(0, 20).map((task) => (
              <div
                key={task.id}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{task.title}</p>
                  <p className="text-xs text-gray-500">
                    {task.project_id} {task.agent_name ? `\u00b7 ${task.agent_name}` : ""}
                  </p>
                </div>
                <StatusBadge status={task.status} />
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StatCard({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-4 rounded-lg border border-gray-800 bg-gray-900 p-4">
      {icon}
      <div>
        <p className="text-sm text-gray-400">{label}</p>
        <p className="text-lg font-semibold">{value}</p>
      </div>
    </div>
  );
}
