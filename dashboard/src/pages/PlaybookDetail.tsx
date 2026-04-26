import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import {
  usePlaybooks,
  usePlaybookSource,
  usePlaybookRuns,
  useUpdatePlaybookSource,
  type PlaybookUpdateResult,
} from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import DeletePlaybookModal from "../components/DeletePlaybookModal";

type TabId = "source" | "compiled" | "runs";

const TABS: { id: TabId; label: string }[] = [
  { id: "source", label: "Source" },
  { id: "compiled", label: "Compiled" },
  { id: "runs", label: "Runs" },
];

export default function PlaybookDetail() {
  const { playbookId = "" } = useParams<{ playbookId: string }>();
  const id = decodeURIComponent(playbookId);
  const [tab, setTab] = useState<TabId>("source");
  const [deleteOpen, setDeleteOpen] = useState(false);

  const { data: playbooks } = usePlaybooks();
  const meta = useMemo(() => playbooks?.find((p) => p.id === id), [playbooks, id]);

  return (
    <div className="space-y-6">
      <Link
        to="/playbooks"
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200"
      >
        <ArrowLeftIcon className="h-4 w-4" /> Back to playbooks
      </Link>

      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">{id}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-sm text-gray-400">
            {meta && (
              <>
                <span>{meta.scope}{meta.scope_identifier ? `:${meta.scope_identifier}` : ""}</span>
                <span>v{meta.version}</span>
                <span>{meta.node_count} nodes</span>
                {(meta.triggers ?? []).map((t) => (
                  <span key={t} className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">
                    {t}
                  </span>
                ))}
                {(meta.running_count ?? 0) > 0 && (
                  <span className="text-green-400">{meta.running_count} running</span>
                )}
              </>
            )}
          </div>
        </div>
        <button
          onClick={() => setDeleteOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-red-500/20 hover:text-red-300"
        >
          <TrashIcon className="h-4 w-4" />
          Delete
        </button>
      </div>

      <DeletePlaybookModal
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        playbookId={id}
      />

      <div className="flex items-center gap-1 border-b border-gray-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              tab === t.id
                ? "border-b-2 border-indigo-400 text-indigo-400"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "source" && <SourceTab playbookId={id} />}
      {tab === "compiled" && <CompiledTab playbookId={id} />}
      {tab === "runs" && <RunsTab playbookId={id} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Source tab — editable textarea wired to sync save-and-compile
// ---------------------------------------------------------------------------

function SourceTab({ playbookId }: { playbookId: string }) {
  const { data: source, isLoading, refetch } = usePlaybookSource(playbookId);
  const update = useUpdatePlaybookSource();

  const [draft, setDraft] = useState("");
  const [baseHash, setBaseHash] = useState("");
  const [lastResult, setLastResult] = useState<PlaybookUpdateResult | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (source) {
      setDraft(source.markdown);
      setBaseHash(source.source_hash);
      setLastResult(null);
      setSaveError(null);
    }
  }, [source]);

  const dirty = source ? draft !== source.markdown : false;

  const onSave = async () => {
    setSaveError(null);
    setLastResult(null);
    try {
      const result = await update.mutateAsync({
        playbook_id: playbookId,
        markdown: draft,
        expected_source_hash: baseHash,
      });
      setLastResult(result);
      if (result.source_hash) setBaseHash(result.source_hash);
      if (result.error === "conflict") {
        setSaveError(
          "Vault changed underneath this editor. Reload to pick up the latest, or overwrite by saving again without the hash.",
        );
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  };

  if (isLoading) return <p className="text-sm text-gray-500">Loading source...</p>;
  if (!source) return <p className="text-sm text-gray-500">Source unavailable.</p>;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs text-gray-500">
        <span className="truncate font-mono">{source.path}</span>
        <span>hash {baseHash.slice(0, 12)}</span>
      </div>

      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        className="h-[60vh] w-full resize-none rounded-lg border border-gray-800 bg-gray-900 p-4 font-mono text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
      />

      <div className="flex items-center gap-3">
        <button
          onClick={onSave}
          disabled={!dirty || update.isPending}
          className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
        >
          {update.isPending ? "Saving..." : dirty ? "Save & Compile" : "Saved"}
        </button>
        <button
          onClick={() => {
            setDraft(source.markdown);
            setSaveError(null);
            setLastResult(null);
          }}
          disabled={!dirty || update.isPending}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Revert
        </button>
        <button
          onClick={() => refetch()}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
        >
          Reload
        </button>
      </div>

      {saveError && (
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{saveError}</span>
        </div>
      )}

      {lastResult && lastResult.compiled && (
        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">
          <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Compiled v{lastResult.version} — {lastResult.node_count} nodes
            {lastResult.retries_used ? ` (${lastResult.retries_used} retries)` : ""}.
          </span>
        </div>
      )}

      {lastResult && !lastResult.compiled && lastResult.errors && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
          <div className="mb-2 flex items-center gap-2 font-medium">
            <ExclamationTriangleIcon className="h-4 w-4" />
            Validation failed — previous compiled version still live
            {lastResult.retries_used ? ` (${lastResult.retries_used} retries)` : ""}.
          </div>
          <ul className="ml-4 list-disc space-y-1">
            {lastResult.errors.map((e, i) => (
              <li key={i} className="font-mono text-xs">
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Compiled tab — read-only JSON view
// ---------------------------------------------------------------------------

function CompiledTab({ playbookId }: { playbookId: string }) {
  const { data: playbooks } = usePlaybooks();
  const meta = playbooks?.find((p) => p.id === playbookId);

  if (!meta) return <p className="text-sm text-gray-500">Compiled data unavailable.</p>;

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Compiled metadata from the active registry. For full node details, use the Graph tab
        (coming soon) or the compiled JSON on disk.
      </p>
      <pre className="overflow-x-auto rounded-lg border border-gray-800 bg-gray-900 p-4 font-mono text-xs text-gray-300">
        {JSON.stringify(meta, null, 2)}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Runs tab — list of recent runs for this playbook
// ---------------------------------------------------------------------------

function RunsTab({ playbookId }: { playbookId: string }) {
  const { data: runs, isLoading } = usePlaybookRuns(playbookId);

  if (isLoading) return <p className="text-sm text-gray-500">Loading runs...</p>;
  if (!runs?.length)
    return <p className="text-sm text-gray-500">No runs recorded for this playbook.</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">Run ID</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Version</th>
            <th className="px-4 py-3">Current node</th>
            <th className="px-4 py-3">Path</th>
            <th className="px-4 py-3">Duration</th>
            <th className="px-4 py-3">Tokens</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {runs.map((r) => (
            <tr key={r.run_id} className="hover:bg-gray-900/50">
              <td className="px-4 py-3 font-mono text-xs text-gray-300">
                {r.run_id.slice(0, 12)}
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={r.status} />
              </td>
              <td className="px-4 py-3 text-gray-400">v{r.playbook_version}</td>
              <td className="px-4 py-3 text-gray-400">{r.current_node ?? "—"}</td>
              <td className="px-4 py-3 text-gray-400">
                <span className="font-mono text-xs">
                  {(r.path ?? []).map((n) => n.node_id).join(" → ") || "—"}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-400">
                {r.duration_seconds != null ? `${r.duration_seconds.toFixed(2)}s` : "—"}
              </td>
              <td className="px-4 py-3 text-gray-400">{r.tokens_used ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
