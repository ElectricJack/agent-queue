import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useFlockSessions } from "../../api/agents";
import { sessionPrune } from "../../api/client";

function age(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return value + "s";
  if (value < 3600) return Math.floor(value / 60) + "m";
  return Math.floor(value / 3600) + "h";
}

export default function SessionDirectory() {
  const [includeStopped, setIncludeStopped] = useState(false);
  const [state, setState] = useState("all");
  const { data: sessions = [], isLoading, error } = useFlockSessions(includeStopped);
  const client = useQueryClient();
  const prune = useMutation({
    mutationFn: async (sessionId: string) => (await sessionPrune({
      body: { session_id: sessionId }, throwOnError: true,
    })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["agents"] });
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const rows = sessions.filter((session) => state === "all" || session.state === state);
  return (
    <section aria-label="All flock sessions" className="rounded-xl border border-gray-800 p-3">
      <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-gray-400">
        <h2 className="mr-auto font-medium text-gray-200">Sessions ({rows.length})</h2>
        <label>State{" "}
          <select aria-label="Session state" value={state} onChange={(event) => setState(event.target.value)}
            className="rounded border border-gray-700 bg-gray-900 p-1">
            {["all", "starting", "running", "draining", "sleeping", ...(includeStopped ? ["stopped", "quarantined"] : [])]
              .map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={includeStopped} onChange={(event) => {
            setIncludeStopped(event.target.checked); setState("all");
          }} />Show stopped history
        </label>
      </div>
      {(error || prune.error) && <p role="alert" className="text-xs text-red-300">{(error || prune.error)?.message}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs text-gray-400">
          <thead><tr>{["Session", "Role", "Scope", "Provider / model / class", "Task", "State", "Uptime", "Last activity", ""]
            .map((label, index) => <th key={index} className="p-2 font-medium">{label}</th>)}</tr></thead>
          <tbody>{rows.map((session) => <tr key={session.session_id} className="border-t border-gray-800">
            <td className="p-2"><Link to={"/sessions/" + session.session_id} className="text-indigo-300 hover:underline">{session.name}</Link></td>
            <td className="p-2">{session.role}</td><td className="p-2">{session.scope}</td>
            <td className="p-2">{[session.provider, session.model, session.intelligence_class].map((value) => value || "unknown").join(" / ")}</td>
            <td className="p-2">{session.task_id ? <Link to={"/tasks/" + session.task_id} className="text-indigo-300">{session.task_id}</Link> : "—"}</td>
            <td className="p-2">{session.state}</td><td className="p-2">{age(session.uptime_seconds)}</td>
            <td className="p-2">{session.last_activity ? age(Date.now() / 1000 - session.last_activity) + " ago" : "unknown"}</td>
            <td className="p-2">{session.lifecycle === "named" && !session.task_id && ["sleeping", "stopped"].includes(session.state)
              && <button type="button" aria-label={"Clean up " + session.name} disabled={prune.isPending}
                onClick={() => prune.mutate(session.session_id)} className="text-gray-300 underline disabled:opacity-50">Clean up</button>}</td>
          </tr>)}</tbody>
        </table>
      </div>
      {!rows.length && <p className="p-2 text-xs text-gray-500">{isLoading ? "Loading sessions…" : "No sessions match this filter."}</p>}
    </section>
  );
}
