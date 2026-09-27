import { useSearchParams } from "react-router-dom";
import ProviderUsage from "../metrics/ProviderUsage";
import { TaskWorkspaceProvider } from "../command-center/TaskWorkspace";
import ActiveSessions from "./ActiveSessions";
import FocusTaskList from "./FocusTaskList";
import { useFocusChrome } from "./focusChrome";

/**
 * `/focus` — the operator's must-haves on one page (mobile dashboard §4.2):
 * live terminals, provider quota with age (the cards, not the chart bundle —
 * ProviderUsage imports no uPlot), and the paged task list. It reuses the
 * existing queries and event invalidation; there is no phone polling loop.
 */
export default function FocusHome() {
  const [params] = useSearchParams();
  const projectId = params.get("project") || undefined;
  useFocusChrome({ title: "Agent Q", fullHref: "/command-center" });
  return (
    <div className="space-y-6 p-3">
      <ActiveSessions />
      <ProviderUsage />
      <TaskWorkspaceProvider scope={{ projectId }}>
        <FocusTaskList />
      </TaskWorkspaceProvider>
    </div>
  );
}
