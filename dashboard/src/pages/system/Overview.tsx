import {
  BoltIcon,
  CpuChipIcon,
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  PauseIcon,
  PlayIcon,
} from "@heroicons/react/24/outline";
import { Link } from "react-router-dom";
import {
  useAllAgents,
  useActiveTasksAllProjects,
  useHealth,
  useOrchestratorControl,
  useOrchestratorStatus,
  useProjects,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";

export default function SystemOverview() {
  const health = useHealth();
  const projects = useProjects();
  const projectIds = (projects.data ?? []).map((p) => p.id);
  const agents = useAllAgents(projectIds);
  const activeTasks = useActiveTasksAllProjects();
  const orchStatus = useOrchestratorStatus();
  const orchControl = useOrchestratorControl();

  const agentList = agents.data ?? [];
  const taskList = activeTasks.data ?? [];
  const busyAgents = agentList.filter((a) => a.state === "busy").length;
  const failedTasks = taskList.filter((t) => t.status === "failed").length;
  const isPaused = orchStatus.data?.status === "paused";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">System</h1>
        <OrchestratorControl
          status={orchStatus.data?.status}
          isLoading={orchStatus.isLoading}
          isPending={orchControl.isPending}
          onToggle={() => orchControl.mutate(isPaused ? "resume" : "pause")}
        />
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={<BoltIcon className="h-5 w-5 text-green-400" />}
          label="Health"
          value={health.data?.status ?? "..."}
        />
        <StatCard
          icon={<CpuChipIcon className="h-5 w-5 text-indigo-400" />}
          label="Active Workspaces"
          value={`${busyAgents} / ${agentList.length}`}
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

      {/* Projects */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Projects</h2>
        {(projects.data ?? []).length === 0 ? (
          <p className="text-sm text-gray-500">No projects.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {(projects.data ?? []).map((p) => {
              const projectAgents = agentList.filter((a) => a.project_id === p.id);
              const projectTasks = taskList.filter((t) => t.project_id === p.id);
              return (
                <Link
                  key={p.id}
                  to={`/projects/${p.id}`}
                  className="rounded-lg border border-gray-800 bg-gray-900 p-4 transition-colors hover:border-indigo-500/50"
                >
                  <div className="mb-1 flex items-center justify-between">
                    <span className="font-medium">{p.name || p.id}</span>
                    {p.paused && (
                      <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs text-amber-400">
                        paused
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500">
                    {projectAgents.length} workspaces · {projectTasks.length} active tasks
                  </p>
                </Link>
              );
            })}
          </div>
        )}
      </section>

      {/* Active tasks across projects */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Active Tasks</h2>
        {taskList.length === 0 ? (
          <p className="text-sm text-gray-500">No active tasks.</p>
        ) : (
          <div className="space-y-2">
            {taskList.slice(0, 20).map((task) => (
              <Link
                key={task.id}
                to={`/tasks/${task.id}`}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-3 transition-colors hover:border-indigo-500/50"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{task.title}</p>
                  <p className="text-xs text-gray-500">
                    {task.project_id} {task.agent_name ? `\u00b7 ${task.agent_name}` : ""}
                  </p>
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

function OrchestratorControl({
  status,
  isLoading,
  isPending,
  onToggle,
}: {
  status?: "paused" | "running";
  isLoading: boolean;
  isPending: boolean;
  onToggle: () => void;
}) {
  if (isLoading || !status) {
    return <div className="h-8 w-8 animate-pulse rounded-md bg-gray-800" />;
  }
  const paused = status === "paused";
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={isPending}
      className={`inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        paused
          ? "bg-amber-500/10 text-amber-400 hover:bg-amber-500/20"
          : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
      }`}
      title={paused ? "Resume orchestrator" : "Pause orchestrator"}
      aria-label={paused ? "Resume orchestrator" : "Pause orchestrator"}
    >
      {paused ? <PlayIcon className="h-4 w-4" /> : <PauseIcon className="h-4 w-4" />}
    </button>
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
