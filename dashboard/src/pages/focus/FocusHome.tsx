import { Link, useSearchParams } from "react-router-dom";
import ProviderUsage from "../metrics/ProviderUsage";
import { TaskWorkspaceProvider } from "../command-center/TaskWorkspace";
import ActiveSessions from "./ActiveSessions";
import FocusTaskList from "./FocusTaskList";
import { useFocusChrome } from "./focusChrome";
import { FOCUS_CONVERSATIONS, FOCUS_INBOX } from "./routes";

const ENTRY = "block rounded-lg border border-gray-800 px-3 py-2 text-sm font-medium text-gray-100 hover:bg-gray-900";

/**
 * `/focus` — the operator's must-haves on one page (mobile dashboard §4.2):
 * live terminals, provider quota with age (the cards, not the chart bundle —
 * ProviderUsage imports no uPlot), and the paged task list. It reuses the
 * existing queries and event invalidation; there is no phone polling loop.
 *
 * The two rows at the top are the entry points to the routes a Discord post
 * links to (spec §6.1): the needs-you inbox and the supervisor's
 * conversations. Plain links, so they cost no query of their own.
 */
export default function FocusHome() {
  const [params] = useSearchParams();
  const projectId = params.get("project") || undefined;
  useFocusChrome({ title: "Agent Q", fullHref: "/command-center" });
  return (
    <div className="space-y-6 p-3">
      <nav className="grid grid-cols-2 gap-2">
        <Link to={FOCUS_INBOX} className={ENTRY}>Needs you</Link>
        <Link to={FOCUS_CONVERSATIONS} className={ENTRY}>Conversations</Link>
      </nav>
      <ActiveSessions />
      <ProviderUsage />
      <TaskWorkspaceProvider scope={{ projectId }}>
        <FocusTaskList />
      </TaskWorkspaceProvider>
    </div>
  );
}