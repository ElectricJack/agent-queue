import { Link, useLocation, useParams } from "react-router-dom";
import TaskFilesPanel from "../components/TaskFilesPanel";
import { useTask } from "../api/hooks";
import { workspaceHref } from "../shell/projectNavigation";

export default function TaskFiles() {
  const { taskId = "" } = useParams();
  const location = useLocation();
  const { data: task } = useTask(taskId);
  const from = (location.state as { from?: string } | null)?.from ?? workspaceHref(task?.project_id, "tasks");
  return (
    <div className="h-full overflow-y-auto p-6 space-y-4">
      <Link to={from} className="text-sm text-indigo-400 hover:underline">Back to tasks</Link>
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Task files</p>
        <h1 className="text-2xl font-bold">Worktree preview</h1>
        <p className="text-xs text-gray-500">task: <span className="font-mono">{taskId}</span></p>
      </header>
      <TaskFilesPanel taskId={taskId} />
    </div>
  );
}
