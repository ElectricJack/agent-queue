import { Navigate, useLocation, useParams } from "react-router-dom";
import { workspaceHref } from "../../shell/projectNavigation";

/** Replace retired tabs without dropping their filters or historical selections. */
export default function RecordsRedirect({ kind }: { kind?: "task" | "knowledge" }) {
  const { projectId } = useParams();
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  if (kind && !params.has("kind")) params.set("kind", kind);
  if (kind === "knowledge" && params.has("record") && !params.has("recordKind")) {
    params.set("recordKind", "knowledge");
  }
  const restore = (location.state as { restoreTaskPane?: { taskId?: string } } | null)?.restoreTaskPane;
  if (restore?.taskId && !params.has("task") && !params.has("record")) params.set("task", restore.taskId);
  return <Navigate to={workspaceHref(projectId, "tasks-knowledge", params.size ? `?${params}` : "")}
    state={location.state} replace />;
}
