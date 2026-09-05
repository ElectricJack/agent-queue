import { PlusIcon, UsersIcon, XMarkIcon } from "@heroicons/react/24/outline";
import { useAgentFlock } from "../../api/agents";
import { selectionAddress, useAgentSelection } from "./useAgentSelection";
import AgentWindow from "./AgentWindow";
import AddAgent from "./AddAgent";
import AddPool from "./AddPool";
import CreateChoice from "./CreateChoice";
import PoolWindow from "./PoolWindow";
import { usePoolFlock } from "./pools";

export default function AgentWorkspace() {
  const { data: agents = [], isLoading, error, refetch } = useAgentFlock();
  const { selectedIds, selections, select, close, setInstance, resetToken, adding, setAdding } = useAgentSelection();
  const { entries: pools } = usePoolFlock();
  const columns = selectedIds.length > 1 ? "lg:grid-cols-2" : "grid-cols-1";
  const rows = selectedIds.length > 2 ? "lg:grid-rows-2" : "lg:grid-rows-1";

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      {/* The header is empty-state chrome only: a selected agent gets the full height. */}
      {selectedIds.length === 0 && (
      <header className="flex shrink-0 items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-gray-100">Agent flock</h1>
          <p className="mt-0.5 text-xs text-gray-500">Global workers shared across projects. Shift-click agents to tile up to four views.</p>
        </div>
        <button type="button" aria-label="Create agent or pool" aria-expanded={adding !== null}
          onClick={() => setAdding(adding ? null : "choice")}
          className="flex shrink-0 items-center gap-1.5 rounded border border-gray-700 px-3 py-2 text-xs text-gray-300 hover:bg-gray-800">
          <PlusIcon className="h-3.5 w-3.5" />Create
        </button>
      </header>
      )}
      {adding === "choice" && <CreateChoice onChoose={setAdding} onCancel={() => setAdding(null)} />}
      {adding === "agent" && (
        <AddAgent onCancel={() => setAdding(null)} onCreated={(id) => select(id)}
          onSwitchToPool={() => setAdding("pool")} />
      )}
      {adding === "pool" && (
        <AddPool onCancel={() => setAdding(null)} onCreated={(poolKey) => select(poolKey)} />
      )}
      {error && (
        <div role="alert" className="rounded border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load the agent flock: {error.message}{" "}
          <button type="button" className="underline" onClick={() => void refetch()}>Retry</button>
        </div>
      )}
      {selectedIds.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-gray-800 p-8 text-center">
          <UsersIcon className="h-10 w-10 text-gray-700" />
          <p className="text-sm text-gray-400">Select an agent from the flock to open its live terminal and settings.</p>
          <p className="text-xs text-gray-600">Closing a view never stops the agent.</p>
        </div>
      ) : (
        <div className={"grid min-h-0 flex-1 auto-rows-[minmax(20rem,1fr)] gap-3 overflow-y-auto lg:auto-rows-auto " + columns + " " + rows}>
          {selections.map((selection) => {
            const id = selection.key;
            if (selection.kind === "pool") {
              const entry = pools.find((pool) => pool.key === selectionAddress(id));
              if (entry) {
                return (
                  <PoolWindow key={entry.key} entry={entry} instanceId={selection.instanceId}
                    onInstanceChange={(instanceId) => setInstance(id, instanceId)}
                    onClose={() => close(id)} resetToken={resetToken} />
                );
              }
            }
            const agent = selection.kind === "agent" ? agents.find((item) => item.id === selection.agentId) : undefined;
            return agent ? (
              <AgentWindow key={id} agent={agent} onClose={() => close(id)} resetToken={resetToken} />
            ) : (
              <section key={id} aria-label={id + " agent window"}
                className="flex min-h-80 flex-col rounded-xl border border-gray-800 p-4 lg:min-h-0">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate font-mono text-xs text-gray-400">{id}</h2>
                  <button type="button" aria-label={"Close " + id + " view"} onClick={() => close(id)} className="rounded p-1 text-gray-500 hover:bg-gray-800">
                    <XMarkIcon className="h-4 w-4" />
                  </button>
                </div>
                <p className="m-auto text-sm text-gray-500">{isLoading ? "Loading agent…" : error ? "Agent unavailable."
                    : selection.kind === "pool" ? "This pool is no longer configured." : "Agent not found."}</p>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
