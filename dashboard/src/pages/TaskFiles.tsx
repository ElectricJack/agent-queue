import { useParams } from "react-router-dom";
import TaskFilesPanel from "../components/TaskFilesPanel";

export default function TaskFiles() {
  const { taskId = "" } = useParams();
  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Task files</p>
        <h1 className="text-2xl font-bold">Worktree preview</h1>
        <p className="text-xs text-gray-500">task: <span className="font-mono">{taskId}</span></p>
      </header>
      <TaskFilesPanel taskId={taskId} />
    </div>
  );
}
