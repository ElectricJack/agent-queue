import { useState } from "react";
import { CheckIcon } from "@heroicons/react/24/outline";
import { useGates, useResolveGate, type GateSummary } from "../../api/hooks";

const STATUSES = ["open", "resolved", "expired"] as const;

function GateRow({ gate }: { gate: GateSummary }) {
  const resolve = useResolveGate();
  const [resolution, setResolution] = useState("");
  const [showForm, setShowForm] = useState(false);

  return (
    <tr className="hover:bg-gray-900">
      <td className="px-3 py-2 font-mono text-xs text-gray-400">{gate.id}</td>
      <td className="px-3 py-2 text-gray-400">{gate.project_id}</td>
      <td className="px-3 py-2 text-gray-300">{gate.gate_type}</td>
      <td className="px-3 py-2">
        <div className="text-gray-200">{gate.title}</div>
        {gate.question && <div className="text-xs text-gray-500">{gate.question}</div>}
      </td>
      <td className="px-3 py-2">
        <span
          className={`rounded px-2 py-0.5 text-xs ${
            gate.status === "open"
              ? "bg-amber-500/10 text-amber-400"
              : gate.status === "resolved"
                ? "bg-emerald-500/10 text-emerald-400"
                : "bg-gray-800 text-gray-400"
          }`}
        >
          {gate.status}
        </span>
      </td>
      <td className="px-3 py-2 text-right">
        {gate.status === "open" && (
          <>
            {!showForm && (
              <button
                onClick={() => setShowForm(true)}
                className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500"
              >
                <CheckIcon className="h-3 w-3" />
                Resolve
              </button>
            )}
            {showForm && (
              <form
                className="flex gap-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  resolve.mutate(
                    {
                      gate_id: gate.id,
                      resolved_by: "dashboard",
                      resolution,
                    },
                    { onSuccess: () => setShowForm(false) },
                  );
                }}
              >
                <input
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  placeholder="Resolution note…"
                  className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs text-gray-200"
                />
                <button
                  type="submit"
                  disabled={resolve.isPending}
                  className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  OK
                </button>
                <button
                  type="button"
                  onClick={() => setShowForm(false)}
                  className="rounded border border-gray-800 px-2 py-1 text-xs text-gray-400"
                >
                  Cancel
                </button>
              </form>
            )}
          </>
        )}
      </td>
    </tr>
  );
}

export default function SystemGates() {
  const [status, setStatus] = useState<string>("open");
  const { data: gates = [], isLoading, error } = useGates({ status });

  return (
    <div className="space-y-4">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold">Gates</h1>
          <p className="text-sm text-gray-500">
            Work-graph gates awaiting resolution — resolving one may unblock waiter tasks.
          </p>
        </div>
        <div className="flex gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`rounded px-3 py-1 text-xs font-medium ${
                s === status
                  ? "bg-indigo-600 text-white"
                  : "border border-gray-800 text-gray-400 hover:bg-gray-900"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </header>

      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && (
        <p className="text-sm text-red-400">
          Failed to load gates: {(error as Error).message}
        </p>
      )}

      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">ID</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Title / Question</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {gates.map((g) => (
              <GateRow key={g.id} gate={g} />
            ))}
            {gates.length === 0 && !isLoading && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                  No gates.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
