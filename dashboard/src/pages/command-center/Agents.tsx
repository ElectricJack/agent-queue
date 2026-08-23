import { useAllAgents, useProjects, useSessions } from "../../api/hooks";
import { useShellPaneStore } from "../../panes/store";
import { useListNav } from "../../shell/hotkeys/useListNav";

export default function CommandCenterAgents() {
  const { data: projects } = useProjects();
  const ids = (projects ?? []).map((p) => p.id);
  const { data: agents = [], isLoading } = useAllAgents(ids);
  const { data: sessions = [] } = useSessions();
  const pane = useShellPaneStore();
  const bodyRef = useListNav<HTMLTableSectionElement>({ axis: "vertical" });

  // Build agent→session lookup: prefer the session that currently owns the
  // agent's task, else fall back to a session with a matching name.
  const sessionForAgent = (a: { name?: string; current_task_id?: string | null }) => {
    if (!a.name) return undefined;
    if (a.current_task_id) {
      const byTask = sessions.find((s) => s.task_id === a.current_task_id);
      if (byTask) return byTask;
    }
    return sessions.find((s) => s.name?.includes(a.name!));
  };

  const openAgentRow = (agentName: string | undefined) => {
    if (!agentName) return;
    const a = agents.find((x) => x.name === agentName);
    if (!a) return;
    const s = sessionForAgent(a);
    if (s) pane.open("session-peek", { sessionId: s.id });
  };

  const handleKeyDown =
    (agentName: string | undefined) =>
    (e: React.KeyboardEvent<HTMLTableRowElement>) => {
      switch (e.key) {
        case "o":
        case "Enter":
        case "p":
        case "k":
          e.preventDefault();
          openAgentRow(agentName);
          break;
      }
    };

  return (
    <div className="space-y-4 p-4">
      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-900 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Session</th>
            </tr>
          </thead>
          <tbody ref={bodyRef} className="divide-y divide-gray-800">
            {isLoading && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && agents.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-center text-gray-500">
                  No agents.
                </td>
              </tr>
            )}
            {agents.map((a) => {
              const s = sessionForAgent(a);
              return (
                <tr
                  key={`${a.project_id}:${a.name}`}
                  tabIndex={0}
                  data-listnav="1"
                  onKeyDown={handleKeyDown(a.name)}
                  onClick={() => openAgentRow(a.name)}
                  className="cursor-pointer hover:bg-gray-900/50 focus:bg-gray-900/50 focus:outline-none"
                >
                  <td className="px-3 py-2 text-gray-200">{a.name}</td>
                  <td className="px-3 py-2 text-gray-300">{a.state}</td>
                  <td className="px-3 py-2 text-gray-400">{a.project_id}</td>
                  <td className="px-3 py-2 text-gray-400">
                    {a.current_task_id ?? "-"}
                  </td>
                  <td className="px-3 py-2 text-gray-400">
                    {s ? (
                      <span className="font-mono text-xs">
                        {s.id?.slice(0, 8)} · {s.state}
                      </span>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
