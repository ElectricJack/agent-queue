import { Link, useParams } from "react-router-dom";
import {
  CpuChipIcon,
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import { useAgents, useTasks, useWorkspaces } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";

export default function ProjectOverview() {
  const { projectId = "" } = useParams();
  const { data: tasks } = useTasks(projectId);
  const { data: agents } = useAgents(projectId);
  const { data: workspaces } = useWorkspaces(projectId);

  const taskList = tasks ?? [];
  const agentList = agents ?? [];
  const workspaceList = workspaces ?? [];
  const activeTasks = taskList.filter(
    (t) => !["COMPLETED", "FAILED", "SKIPPED"].includes((t.status ?? "").toUpperCase()),
  );
  const failedTasks = taskList.filter((t) => (t.status ?? "").toUpperCase() === "FAILED");
  const busy = agentList.filter((a) => a.state === "busy").length;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          icon={<CpuChipIcon className="h-5 w-5 text-indigo-400" />}
          label="Workspaces"
          value={`${busy} busy / ${workspaceList.length}`}
        />
        <StatCard
          icon={<ClipboardDocumentListIcon className="h-5 w-5 text-blue-400" />}
          label="Active Tasks"
          value={String(activeTasks.length)}
        />
        <StatCard
          icon={<ExclamationTriangleIcon className="h-5 w-5 text-red-400" />}
          label="Failed"
          value={String(failedTasks.length)}
        />
      </div>

      <section>
        <h2 className="mb-3 text-lg font-semibold">Active Tasks</h2>
        {activeTasks.length === 0 ? (
          <p className="text-sm text-gray-500">No active tasks.</p>
        ) : (
          <div className="space-y-2">
            {activeTasks.slice(0, 10).map((task) => (
              <Link
                key={task.id}
                to={`/tasks/${task.id}`}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-3 transition-colors hover:border-indigo-500/50"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{task.title}</p>
                  {task.agent_name && (
                    <p className="text-xs text-gray-500">{task.agent_name}</p>
                  )}
                </div>
                <StatusBadge status={task.status} />
              </Link>
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
