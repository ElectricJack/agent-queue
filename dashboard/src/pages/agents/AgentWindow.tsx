import { useEffect, useId, useState } from "react";
import { CommandLineIcon, Cog6ToothIcon, ArrowPathIcon } from "@heroicons/react/24/outline";
import { useRestartSupervisor, type FlockAgent } from "../../api/agents";
import { AgentSubagents, AgentState, AgentEligibility } from "./AgentMetadata";
import AgentSettings from "./AgentSettings";
import AgentTerminal from "./AgentTerminal";
import TerminalPane, { TerminalTabs } from "../../components/TerminalPane";

export default function AgentWindow({ agent, onClose, resetToken, focusRequest }: {
  agent: FlockAgent;
  onClose: () => void;
  resetToken: string | null;
  focusRequest: string | null;
}) {
  const [tab, setTab] = useState<"terminal" | "settings">("terminal");
  const [resume, setResume] = useState(false);
  const restart = useRestartSupervisor();
  const supervisor = agent.role === "supervisor" || agent.id === "supervisor-global";
  const id = useId();
  useEffect(() => {
    if (resetToken || focusRequest) setTab("terminal");
  }, [resetToken, focusRequest]);

  const tabs = [
    { id: "terminal" as const, label: "Terminal", Icon: CommandLineIcon },
    { id: "settings" as const, label: "Settings", Icon: Cog6ToothIcon },
  ];

  return (
    <section aria-label={agent.name + " agent window"}
      className="flex min-h-96 min-w-0 flex-col overflow-hidden rounded-xl border border-gray-800 bg-gray-900/40 lg:min-h-0">
      <TerminalPane title={agent.name} status={<AgentState agent={agent} />} onClose={onClose} titleId={id + "-title"}
        tabs={<TerminalTabs label={agent.name + " view"} idPrefix={id} tabs={tabs} value={tab} onChange={setTab} />} details={<>
        <div className="space-y-1">
          <div>Role: {agent.role || "worker"}</div>
          <div>Profile: {agent.profile_id}</div>
          <div>{agent.provider || "Provider unknown"} · {agent.model || "Model unknown"}</div>
          <div>Intelligence: {agent.intelligence_class || "Unknown"}</div>
          <div><AgentSubagents agent={agent} /></div>
          <div><AgentEligibility agent={agent} /></div>
          <div>Task: {agent.current_task_title || agent.current_task_id || (supervisor ? "Supervises all AQ projects" : "Idle — no assigned task")}</div>
          {agent.current_task_id && <div>Task ID: {agent.current_task_id}</div>}
          <div>Session: {agent.session_id || "No active session"} · {agent.session_state || "unknown"}</div>
          {!supervisor && agent.project_id && <div>Attached project: {agent.project_id}</div>}
        </div>
        {agent.role === "supervisor" && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select data-primary-control aria-label="Restart conversation" value={resume ? "resume" : "fresh"}
              disabled={restart.isPending}
              onChange={(event) => { setResume(event.target.value === "resume"); restart.reset(); }}
              className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-300">
              <option value="fresh">Fresh conversation</option>
              <option value="resume">Resume conversation</option>
            </select>
            <button type="button" data-primary-control disabled={restart.isPending || !agent.session_id}
              onClick={() => restart.mutate({
                name: "supervisor-global", resume, session_id: agent.session_id!,
              })}
              className="flex items-center gap-1.5 rounded border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800 disabled:opacity-50">
              <ArrowPathIcon className={"h-3.5 w-3.5" + (restart.isPending ? " animate-spin" : "")} />
              {restart.isPending ? "Restarting…" : "Restart"}
            </button>
          </div>
        )}
        {restart.isError && <p role="alert" className="mt-2 text-xs text-red-400">{restart.error.message}</p>}
        {restart.isSuccess && <p role="status" className="mt-2 text-xs text-gray-300">
          Supervisor restarted {restart.data.mode === "resume" ? "with the prior conversation" : "with a fresh conversation"}.
        </p>}
      </>}>
      <div role="tabpanel" id={id + "-panel"} aria-labelledby={id + "-title"} className="min-h-0 flex-1 overflow-hidden">
        {tab === "terminal" ? <AgentTerminal agent={agent} focusRequest={focusRequest} /> : <AgentSettings agent={agent} onDeleted={onClose} />}
      </div>
      </TerminalPane>
    </section>
  );
}
