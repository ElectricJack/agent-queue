import { useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTask } from "../../api/hooks";
import TaskDetailBody from "../../panes/task-detail/TaskDetailBody";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { useFocusBack } from "./useFocusBack";
import { focusTaskHref } from "./routes";

/** `/focus/tasks/:taskId` — the shared task detail, full view, no sidebar (spec §4.1). */
export default function FocusTask() {
  const { taskId = "" } = useParams();
  return <FocusTaskContent key={taskId} taskId={taskId} />;
}

function FocusTaskContent({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const back = useFocusBack();
  // The body reads the same query key, so this is one fetch, not two.
  const query = useTask(taskId);
  const title = query.data?.title ?? "Task";
  useFocusChrome({ title, fullHref: `/tasks/${encodeURIComponent(taskId)}` });
  const openTask = useCallback((id: string) => navigate(focusTaskHref(id)), [navigate]);
  if (query.isError) {
    return (
      <FocusError
        title="Task"
        message={query.error instanceof Error ? query.error.message : "The task could not be read."}
        onRetry={() => void query.refetch()}
      />
    );
  }
  return <TaskDetailBody taskId={taskId} onOpenTask={openTask} onClose={back} />;
}
