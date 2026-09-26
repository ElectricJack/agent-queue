import { useParams } from "react-router-dom";
import { FocusUnavailable } from "./FocusNotice";

/** Replaced by Task 3 (the shared task detail). */
export default function FocusTask() {
  const { taskId = "" } = useParams();
  return <FocusUnavailable title="Task" fullHref={`/tasks/${encodeURIComponent(taskId)}`} />;
}
