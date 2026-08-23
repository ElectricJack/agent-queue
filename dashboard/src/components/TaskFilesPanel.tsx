/**
 * Task worktree file browser + read-only content pane.
 *
 * Designed to be mounted inside Phase 4's task sidebar AND as a standalone
 * route (Phase 5 ships both; standalone is what makes this phase testable
 * without waiting on Phase 4).
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTaskFiles, fetchTaskFileText } from "../api/taskFiles";
import MarkdownPreview from "./MarkdownPreview";

interface Props {
  taskId: string;
}

export function statusColor(status: string): string {
  switch (status) {
    case "A": return "text-green-400";
    case "D": return "text-red-400";
    case "R":
    case "C": return "text-blue-400";
    default:  return "text-amber-300"; // M and unknown
  }
}

export default function TaskFilesPanel({ taskId }: Props) {
  const [selected, setSelected] = useState<string | null>(null);

  const filesQ = useQuery({
    queryKey: ["taskFiles", taskId],
    queryFn: () => fetchTaskFiles(taskId),
    refetchInterval: 5000,
  });

  const fileQ = useQuery({
    queryKey: ["taskFile", taskId, selected],
    queryFn: () => fetchTaskFileText(taskId, selected!),
    enabled: !!selected,
  });

  if (filesQ.isLoading) {
    return <div className="p-4 text-sm text-gray-500">Loading files…</div>;
  }
  if (filesQ.error) {
    return (
      <div className="p-4 text-sm text-red-400">
        Failed to load files: {(filesQ.error as Error).message}
      </div>
    );
  }
  const data = filesQ.data!;
  if (data.reason === "no_workspace") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task has no attached workspace. Files will appear once the task
        acquires a worktree.
      </div>
    );
  }
  if (data.reason === "not_a_git_checkout") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task workspace ({data.workspace_path}) is not a git checkout.
      </div>
    );
  }
  if (data.files.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        No changes vs {data.base} yet.
      </div>
    );
  }

  const isMd = selected?.toLowerCase().endsWith(".md");

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(240px,320px)_1fr]">
      <div className="rounded border border-gray-800 bg-gray-950">
        <div className="border-b border-gray-800 px-3 py-2 text-xs text-gray-500">
          {data.files.length} file{data.files.length === 1 ? "" : "s"} vs {data.base}
        </div>
        <ul className="max-h-[60vh] overflow-y-auto text-xs">
          {data.files.map((f) => (
            <li key={f.path}>
              <button
                onClick={() => setSelected(f.path)}
                className={
                  "flex w-full items-center gap-2 px-3 py-1 text-left font-mono " +
                  (selected === f.path
                    ? "bg-indigo-950/60"
                    : "hover:bg-gray-900")
                }
              >
                <span className={"w-4 " + statusColor(f.status)}>{f.status}</span>
                <span className="flex-1 truncate text-gray-200">{f.path}</span>
                <span className="text-green-400">+{f.additions}</span>
                <span className="text-red-400">-{f.deletions}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : fileQ.isLoading ? (
          <p className="text-sm text-gray-500">Loading {selected}…</p>
        ) : fileQ.error ? (
          <p className="text-sm text-red-400">
            {(fileQ.error as Error).message}
          </p>
        ) : isMd && fileQ.data?.status === 200 ? (
          <MarkdownPreview source={fileQ.data.text} />
        ) : (
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
            {fileQ.data?.text}
          </pre>
        )}
      </div>
    </div>
  );
}
