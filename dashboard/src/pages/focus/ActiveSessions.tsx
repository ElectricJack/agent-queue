import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useAgentFlock } from "../../api/agents";
import { poolDisplayName, usePoolFlock } from "../agents/pools";
import { focusSessionHref } from "./routes";

const isLive = (state?: string | null) => state === "running" || state === "draining";

interface Card {
  id: string;
  name: string;
  state: string;
  detail: string;
  started?: number | null;
}

/** Live tmux sessions — agents and pool workers — each a link to the phone terminal. */
export default function ActiveSessions() {
  const { data: agents = [], isLoading, error } = useAgentFlock();
  const { entries } = usePoolFlock();
  const cards = useMemo(() => {
    const seen = new Set<string>();
    const out: Card[] = [];
    for (const agent of agents) {
      if (!agent.session_id || !isLive(agent.session_state) || seen.has(agent.session_id)) continue;
      if (agent.session_provider && agent.session_provider !== "tmux") continue;
      seen.add(agent.session_id);
      out.push({ id: agent.session_id, name: agent.name, state: agent.session_state ?? "",
        detail: agent.current_task_title || agent.current_task_id || "Idle" });
    }
    for (const entry of entries) {
      for (const instance of entry.instances) {
        if (!isLive(instance.state) || seen.has(instance.id)) continue;
        if (instance.provider && instance.provider !== "tmux") continue;
        seen.add(instance.id);
        // A pool card pins its process: a restart shows a notice, not a stranger.
        out.push({ id: instance.id, name: instance.name, state: instance.state ?? "",
          detail: `Pool ${poolDisplayName(entry.pool)}`, started: instance.started_at });
      }
    }
    return out;
  }, [agents, entries]);

  return (
    <section aria-labelledby="focus-sessions" className="space-y-2">
      <h2 id="focus-sessions" className="text-xs uppercase tracking-wide text-gray-500">Live sessions</h2>
      {error && <p role="alert" className="text-sm text-red-300">Could not load agents.</p>}
      {isLoading && <p role="status" className="text-sm text-gray-500">Loading sessions…</p>}
      {!isLoading && cards.length === 0 && <p className="text-sm text-gray-500">No live sessions.</p>}
      <ul className="grid gap-2 sm:grid-cols-2">
        {cards.map((card) => (
          <li key={card.id}>
            <Link to={focusSessionHref(card.id, { started: card.started })} data-primary-control
              className="block rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2 hover:bg-gray-900">
              <span className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate font-medium text-gray-100">{card.name}</span>
                <span className="shrink-0 text-[11px] text-gray-400">{card.state}</span>
              </span>
              <span className="mt-0.5 line-clamp-2 text-xs text-gray-400 [overflow-wrap:anywhere]">{card.detail}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
