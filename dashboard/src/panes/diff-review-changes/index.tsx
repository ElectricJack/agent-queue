import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  ArrowPathIcon,
  ClipboardIcon,
  ArrowTopRightOnSquareIcon,
} from "@heroicons/react/24/outline";
import { fetchTaskFiles, fetchTaskFileText } from "../../api/taskFiles";
import type { TaskFileEntry } from "../../api/taskFiles";
import { statusColor } from "../../components/TaskFilesPanel";
import MarkdownPreview from "../../components/MarkdownPreview";
import type { PaneViewProps } from "../types";
import type { DiffReviewChangesArgs } from "./manifest";

const NARROW_BREAKPOINT = 400;

export default function DiffReviewChangesPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<DiffReviewChangesArgs>) {
  const navigate = useNavigate();
  const filterInputRef = useRef<HTMLInputElement>(null);
  const [narrow, setNarrow] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [focusedIndex, setFocusedIndex] = useState(0);

  // Narrow-pane collapse: this view measures its own container because
  // PaneViewProps doesn't thread the shell's current pane width down as
  // a prop (diff-review-changes spec §5.1, open question #3).
  //
  // A callback ref (rather than a plain useRef + effect with an empty dep
  // array) is required here: the container div this measures only renders
  // once the file-list query has resolved past the loading/error/reason-code
  // early returns below, so a mount-only effect would find containerEl still
  // null on first paint and never re-run once the real DOM node shows up.
  const [containerEl, setContainerEl] = useState<HTMLDivElement | null>(null);
  const containerRef = useCallback((el: HTMLDivElement | null) => {
    setContainerEl(el);
  }, []);

  useEffect(() => {
    if (!containerEl) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? containerEl.clientWidth;
      setNarrow(width < NARROW_BREAKPOINT);
    });
    observer.observe(containerEl);
    return () => observer.disconnect();
  }, [containerEl]);

  const filesQ = useQuery({
    queryKey: ["taskFiles", args.taskId],
    queryFn: () => fetchTaskFiles(args.taskId),
    refetchInterval: 5000,
  });

  const fileQ = useQuery({
    queryKey: ["taskFile", args.taskId, selected],
    queryFn: () => fetchTaskFileText(args.taskId, selected!),
    enabled: !!selected,
  });

  // filePath arg → initial selection (spec §7.2).
  useEffect(() => {
    if (!args.filePath) return;
    if (!filesQ.data?.files.some((f) => f.path === args.filePath)) return;
    setSelected(args.filePath);
  }, [args.filePath, filesQ.data]);

  function selectFile(f: TaskFileEntry) {
    setSelected(f.path);
    setArgs({ ...args, filePath: f.path });
  }

  const files: TaskFileEntry[] = filesQ.data?.files ?? [];

  const filteredFiles = filter
    ? files.filter((f) => f.path.toLowerCase().includes(filter.toLowerCase()))
    : files;

  function moveSelection(delta: number) {
    setFocusedIndex((idx) => {
      const next = idx + delta;
      if (next < 0) return 0;
      if (next >= filteredFiles.length) return Math.max(filteredFiles.length - 1, 0);
      return next;
    });
  }

  function openFocusedFile() {
    const f = filteredFiles[focusedIndex];
    if (f) selectFile(f);
  }

  // Toolbar + shortcuts register unconditionally on every render, per the
  // plugin-interface contract §5.1/§5.2 — must run before any early return.
  setToolbar([
    {
      id: "refresh",
      label: "Refresh",
      icon: ArrowPathIcon,
      onClick: () => {
        filesQ.refetch();
        if (selected) fileQ.refetch();
      },
    },
    {
      id: "copy-path",
      label: "Copy file path",
      icon: ClipboardIcon,
      onClick: () => navigator.clipboard.writeText(selected ?? ""),
      disabled: !selected,
    },
    {
      id: "open-full-page",
      label: "Open full-page view",
      icon: ArrowTopRightOnSquareIcon,
      onClick: () => navigate(`/tasks/${encodeURIComponent(args.taskId)}/files`),
    },
  ]);

  setShortcuts([
    { key: "ArrowUp", label: "Previous file", onFire: () => moveSelection(-1) },
    { key: "ArrowDown", label: "Next file", onFire: () => moveSelection(1) },
    { key: "Enter", label: "Open file", onFire: openFocusedFile },
    { key: "/", label: "Filter files", onFire: () => filterInputRef.current?.focus() },
    { key: "r", label: "Refresh", onFire: () => filesQ.refetch() },
  ]);

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

  const data = filesQ.data;
  if (data?.reason === "no_workspace") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task has no attached workspace. Files will appear once the task
        acquires a worktree.
      </div>
    );
  }
  if (data?.reason === "not_a_git_checkout") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task workspace ({data.workspace_path}) is not a git checkout.
      </div>
    );
  }
  if (data && data.files.length === 0) {
    return <div className="p-4 text-sm text-gray-500">No changes vs {data.base} yet.</div>;
  }

  const isMd = selected?.toLowerCase().endsWith(".md");

  return (
    <div ref={containerRef} className={"flex h-full " + (narrow ? "flex-col" : "")}>
      <div
        className={
          "flex flex-col rounded border border-gray-800 bg-gray-950 " +
          (narrow ? "max-h-[40%] w-full" : "w-2/5 min-w-[140px]")
        }
      >
        <div className="border-b border-gray-800 p-2">
          <input
            ref={filterInputRef}
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setFocusedIndex(0);
            }}
            placeholder="Filter files…"
            className="w-full rounded bg-gray-900 px-2 py-1 text-xs text-gray-200"
          />
        </div>
        <ul className="flex-1 overflow-y-auto text-xs">
          {filteredFiles.map((f, i) => (
            <li key={f.path}>
              <button
                onClick={() => selectFile(f)}
                className={
                  "flex w-full items-center gap-2 px-3 py-1 text-left font-mono " +
                  (selected === f.path
                    ? "bg-indigo-950/60"
                    : i === focusedIndex
                      ? "bg-gray-900"
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
      <div className="flex-1 rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : fileQ.isLoading ? (
          <p className="text-sm text-gray-500">Loading {selected}…</p>
        ) : fileQ.error ? (
          <p className="text-sm text-red-400">{(fileQ.error as Error).message}</p>
        ) : isMd && fileQ.data?.status === 200 ? (
          <MarkdownPreview source={fileQ.data.text} />
        ) : (
          <pre className="max-h-full overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
            {fileQ.data?.text}
          </pre>
        )}
      </div>
    </div>
  );
}
