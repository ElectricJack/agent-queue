import { useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { PauseIcon, PlayIcon, PlusIcon, TrashIcon } from "@heroicons/react/24/outline";
import {
  usePlaybooks,
  useSetPlaybookEnabled,
  type PlaybookSummary,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import CreatePlaybookModal from "../../components/CreatePlaybookModal";
import DeletePlaybookModal from "../../components/DeletePlaybookModal";

const SCOPE_FILTERS = ["all", "system", "project", "agent-type"] as const;
type ScopeFilter = (typeof SCOPE_FILTERS)[number];

export default function Playbooks() {
  const location = useLocation();
  const from = `${location.pathname}${location.search}`;
  const [scope, setScope] = useState<ScopeFilter>("all");
  const [createOpen, setCreateOpen] = useState(false);
  const { data: playbooks, isLoading } = usePlaybooks(scope === "all" ? undefined : scope);

  const rows = useMemo(() => playbooks ?? [], [playbooks]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Playbooks</h1>
        <button
          onClick={() => setCreateOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          <PlusIcon className="h-4 w-4" />
          New playbook
        </button>
      </div>

      <CreatePlaybookModal open={createOpen} onClose={() => setCreateOpen(false)} />

      <div className="flex items-center gap-1 border-b border-gray-800">
        {SCOPE_FILTERS.map((s) => (
          <button
            key={s}
            onClick={() => setScope(s)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              scope === s
                ? "border-b-2 border-indigo-400 text-indigo-400"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {s === "all" ? "All" : s}
          </button>
        ))}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-gray-500">No playbooks compiled for this scope.</p>
      ) : (
        <PlaybookTable rows={rows} from={from} />
      )}
    </div>
  );
}

function PlaybookTable({ rows, from }: { rows: PlaybookSummary[]; from: string }) {
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const setEnabled = useSetPlaybookEnabled();

  const togglePlaybook = (p: PlaybookSummary) => {
    const newEnabled = p.enabled === false;
    setEnabled.mutate({ playbook_id: p.id, enabled: newEnabled });
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">ID</th>
            <th className="px-4 py-3">Scope</th>
            <th className="px-4 py-3">Triggers</th>
            <th className="px-4 py-3">Nodes</th>
            <th className="px-4 py-3">Version</th>
            <th className="px-4 py-3">Last run</th>
            <th className="px-4 py-3">Running</th>
            <th className="px-4 py-3">Status</th>
            <th className="w-20 px-4 py-3"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {rows.map((p) => {
            const isPaused = p.enabled === false;
            return (
              <tr
                key={p.id}
                className={`hover:bg-gray-900/50 ${isPaused ? "opacity-60" : ""}`}
              >
                <td className="px-4 py-3">
                  <Link
                    to={`/playbooks/${encodeURIComponent(p.id)}`}
                    state={{ from }}
                    className="font-medium text-indigo-400 hover:underline"
                  >
                    {p.id}
                  </Link>
                </td>
                <td className="px-4 py-3">
                  <ScopeBadge scope={p.scope} identifier={p.scope_identifier ?? p.agent_type ?? undefined} />
                </td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {(p.triggers ?? []).length === 0 ? (
                      <span className="text-gray-500">—</span>
                    ) : (
                      (p.triggers ?? []).map((t) => (
                        <span
                          key={t}
                          className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300"
                        >
                          {t}
                        </span>
                      ))
                    )}
                  </div>
                </td>
                <td className="px-4 py-3 text-gray-400">{p.node_count}</td>
                <td className="px-4 py-3 text-gray-400">v{p.version}</td>
                <td className="px-4 py-3">
                  {p.last_run ? <StatusBadge status={p.last_run.status} /> : (
                    <span className="text-gray-500">never</span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-400">
                  {(p.running_count ?? 0) > 0 ? (
                    <span className="text-green-400">{p.running_count}</span>
                  ) : (
                    <span className="text-gray-500">0</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      isPaused
                        ? "bg-amber-500/10 text-amber-300"
                        : "bg-emerald-500/10 text-emerald-300"
                    }`}
                  >
                    {isPaused ? "Paused" : "Active"}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => togglePlaybook(p)}
                      disabled={
                        setEnabled.isPending && setEnabled.variables?.playbook_id === p.id
                      }
                      title={
                        isPaused
                          ? "Resume — let trigger events start new runs"
                          : "Pause — stop new triggers (running instances continue)"
                      }
                      className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-100 disabled:opacity-50"
                    >
                      {isPaused ? (
                        <PlayIcon className="h-3.5 w-3.5" />
                      ) : (
                        <PauseIcon className="h-3.5 w-3.5" />
                      )}
                    </button>
                    <button
                      onClick={() => setDeleteId(p.id)}
                      title="Delete"
                      className="rounded p-1 text-red-400 transition-colors hover:bg-red-500/20"
                    >
                      <TrashIcon className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {deleteId && (
        <DeletePlaybookModal
          open={true}
          onClose={() => setDeleteId(null)}
          playbookId={deleteId}
          returnTo={from}
        />
      )}
    </div>
  );
}

function ScopeBadge({ scope, identifier }: { scope: string; identifier?: string }) {
  const tone =
    scope === "system"
      ? "bg-indigo-500/10 text-indigo-400"
      : scope.startsWith("project")
        ? "bg-emerald-500/10 text-emerald-400"
        : "bg-amber-500/10 text-amber-400";
  const label = identifier ? `${scope}:${identifier}` : scope;
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>
      {label}
    </span>
  );
}
