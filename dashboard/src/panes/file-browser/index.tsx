import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FolderIcon,
  DocumentIcon,
  ArrowUpIcon,
  ArrowPathIcon,
  ClipboardIcon,
  HomeIcon,
} from "@heroicons/react/24/outline";
import {
  fetchWorkspaceBrowse,
  fetchWorkspaceFileText,
  workspaceBrowseKey,
  workspaceFileTextKey,
} from "../../api/workspaceFiles";
import type { WorkspaceBrowseEntry } from "../../api/workspaceFiles";
import MarkdownPreview from "../../components/MarkdownPreview";
import type { PaneViewProps } from "../types";
import type { FileBrowserArgs } from "./manifest";

const NARROW_BREAKPOINT = 480;

function parentPath(path: string): string {
  const segments = path.split("/").filter(Boolean);
  segments.pop();
  return segments.join("/");
}

function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

function breadcrumbSegments(path: string): { label: string; path: string }[] {
  const segments = path.split("/").filter(Boolean);
  const crumbs: { label: string; path: string }[] = [{ label: "root", path: "" }];
  let acc = "";
  for (const seg of segments) {
    acc = acc ? `${acc}/${seg}` : seg;
    crumbs.push({ label: seg, path: acc });
  }
  return crumbs;
}

export default function FileBrowserPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<FileBrowserArgs>) {
  const { workspaceId, path } = args;
  const queryClient = useQueryClient();
  const containerRef = useRef<HTMLDivElement>(null);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const [narrow, setNarrow] = useState(false);

  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [focusedIndex, setFocusedIndex] = useState(0);
  const [lastWorkspaceId, setLastWorkspaceId] = useState(workspaceId);

  // Narrow-pane collapse: measure the view's own container (PaneViewProps
  // doesn't thread the shell's current pane width down as a prop — matches
  // diff-review-changes' approach).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? el.clientWidth;
      setNarrow(width < NARROW_BREAKPOINT);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const browseQ = useQuery({
    queryKey: workspaceBrowseKey(workspaceId, path),
    queryFn: () => fetchWorkspaceBrowse(workspaceId, path),
    staleTime: 10_000,
  });
  const fileQ = useQuery({
    queryKey: workspaceFileTextKey(workspaceId, previewPath),
    queryFn: () => fetchWorkspaceFileText(workspaceId, previewPath as string),
    enabled: previewPath != null,
    staleTime: 10_000,
  });

  // Full reset on workspaceId change: a different workspace is a different
  // tree entirely, so the previewed file and filter no longer apply.
  useEffect(() => {
    if (workspaceId !== lastWorkspaceId) {
      setPreviewPath(null);
      setFilter("");
      setFocusedIndex(0);
      setLastWorkspaceId(workspaceId);
    }
  }, [workspaceId, lastWorkspaceId]);

  // Agent-push fallback: a pushed `path` may point at a file rather than a
  // directory (`browse` 404s "not a directory"). Retry against the parent
  // directory and preview the file that was originally requested.
  useEffect(() => {
    if (browseQ.error && browseQ.error.message.includes("not a directory")) {
      const parent = parentPath(path);
      setPreviewPath(path);
      setArgs({ workspaceId, path: parent });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browseQ.error]);

  const allEntries: WorkspaceBrowseEntry[] = browseQ.data?.entries ?? [];
  const entries = filter
    ? allEntries.filter((e) => e.name.toLowerCase().includes(filter.toLowerCase()))
    : allEntries;

  function openEntry(entry: WorkspaceBrowseEntry) {
    const nextPath = joinPath(path, entry.name);
    if (entry.type === "dir") {
      setFilter("");
      setArgs({ workspaceId, path: nextPath });
    } else {
      setPreviewPath(nextPath);
    }
  }

  function moveSelection(delta: number) {
    setFocusedIndex((idx) => {
      const next = idx + delta;
      if (next < 0) return 0;
      if (next >= entries.length) return Math.max(entries.length - 1, 0);
      return next;
    });
  }

  function openFocusedEntry() {
    const e = entries[focusedIndex];
    if (e) openEntry(e);
  }

  function upOneDir() {
    if (path === "") return;
    setArgs({ workspaceId, path: parentPath(path) });
  }

  function openRoot() {
    if (path === "") return;
    setArgs({ workspaceId, path: "" });
  }

  function refresh() {
    queryClient.invalidateQueries({ queryKey: workspaceBrowseKey(workspaceId, path) });
    if (previewPath != null) {
      queryClient.invalidateQueries({ queryKey: workspaceFileTextKey(workspaceId, previewPath) });
    }
  }

  function copyPath() {
    const target = previewPath ?? path;
    void navigator.clipboard.writeText(target);
  }

  // Toolbar + shortcuts register unconditionally on every render, per the
  // plugin-interface contract — must run before any early return.
  setToolbar([
    { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: refresh },
    { id: "copy-path", label: "Copy path", icon: ClipboardIcon, onClick: copyPath },
    { id: "up", label: "Up one dir", icon: ArrowUpIcon, onClick: upOneDir, disabled: path === "" },
    {
      id: "root",
      label: "Open workspace root",
      icon: HomeIcon,
      onClick: openRoot,
      disabled: path === "",
    },
  ]);

  setShortcuts([
    { key: "Backspace", label: "Up one dir", onFire: upOneDir },
    { key: "/", label: "Focus filter", onFire: () => filterInputRef.current?.focus() },
    { key: "r", label: "Refresh", onFire: refresh },
  ]);

  const crumbs = breadcrumbSegments(path);
  const isMd = previewPath?.toLowerCase().endsWith(".md");

  return (
    <div
      ref={containerRef}
      className={"flex h-full gap-2 p-2 " + (narrow ? "flex-col" : "")}
    >
      <div
        className={
          "flex min-h-0 flex-col rounded border border-gray-800 bg-gray-950 " +
          (narrow ? "max-h-[40%] w-full" : "w-1/2")
        }
      >
        <div className="flex flex-wrap items-center gap-1 border-b border-gray-800 px-2 py-1 text-xs text-gray-400">
          {crumbs.map((c, i) => (
            <span key={c.path}>
              {i > 0 && <span className="mx-1 text-gray-600">/</span>}
              <button
                className="hover:text-gray-100 hover:underline"
                onClick={() => setArgs({ workspaceId, path: c.path })}
              >
                {c.label}
              </button>
            </span>
          ))}
        </div>
        <input
          ref={filterInputRef}
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setFocusedIndex(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") setFilter("");
          }}
          placeholder="Filter files…"
          className="border-b border-gray-800 bg-gray-950 px-2 py-1 text-xs text-gray-200 outline-none"
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
          {browseQ.isLoading ? (
            <p className="p-3 text-sm text-gray-500">Loading…</p>
          ) : browseQ.error ? (
            <div className="p-3 text-sm text-red-400">
              Failed to load directory.{" "}
              <button className="underline" onClick={refresh}>
                Retry
              </button>
            </div>
          ) : browseQ.data?.reason === "no_workspace_path" ? (
            <p className="p-3 text-sm text-gray-500">
              This workspace has no filesystem path yet — nothing to browse.
            </p>
          ) : entries.length === 0 && filter ? (
            <p className="p-3 text-sm text-gray-500">No files match &ldquo;{filter}&rdquo;.</p>
          ) : entries.length === 0 ? (
            <p className="p-3 text-sm text-gray-500">This directory is empty.</p>
          ) : (
            <ul className="text-xs">
              {entries.map((entry, i) => (
                <li key={entry.name}>
                  <button
                    onFocus={() => setFocusedIndex(i)}
                    onClick={() => openEntry(entry)}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowDown") {
                        e.preventDefault();
                        moveSelection(1);
                      } else if (e.key === "ArrowUp") {
                        e.preventDefault();
                        moveSelection(-1);
                      } else if (e.key === "Enter") {
                        openFocusedEntry();
                      }
                    }}
                    className={
                      "flex w-full items-center gap-2 px-3 py-1 text-left " +
                      (i === focusedIndex ? "bg-gray-900" : "hover:bg-gray-900")
                    }
                  >
                    {entry.type === "dir" ? (
                      <FolderIcon className="h-4 w-4 shrink-0 text-indigo-400" />
                    ) : (
                      <DocumentIcon className="h-4 w-4 shrink-0 text-gray-500" />
                    )}
                    <span className="flex-1 truncate font-mono text-gray-200">
                      {entry.name}
                      {entry.is_symlink && <span className="ml-1 text-amber-400">@</span>}
                    </span>
                    {entry.type === "file" && entry.size != null && (
                      <span className="text-gray-500">{entry.size}b</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col rounded border border-gray-800 bg-gray-950 p-3">
        {previewPath == null ? (
          <p className="text-sm text-gray-500">Select a file to preview</p>
        ) : fileQ.isLoading ? (
          <>
            <p className="mb-2 text-xs text-gray-500">{previewPath}</p>
            <p className="text-sm text-gray-500">Loading…</p>
          </>
        ) : fileQ.error ? (
          <div className="text-sm text-red-400">
            Failed to load file.{" "}
            <button className="underline" onClick={refresh}>
              Retry
            </button>
          </div>
        ) : (
          <>
            <p className="mb-2 text-xs text-gray-500">{previewPath}</p>
            <div className="min-h-0 flex-1 overflow-auto">
              {isMd && fileQ.data?.status === 200 ? (
                <MarkdownPreview source={fileQ.data.text} />
              ) : (
                <pre className="max-h-full overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
                  {fileQ.data?.text}
                </pre>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
