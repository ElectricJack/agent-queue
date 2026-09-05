import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
import { useWizard } from "./context";
import { GithubRepositoryStep } from "./GithubRepositoryStep";
import { browseProjectRoot, type ProjectRootBrowseEntry } from "./projectRootsClient";
import { directoryNameError } from "./repositoryValidation";

function breadcrumbSegments(path: string) {
  const segments: { label: string; path: string }[] = [];
  let current = "";
  for (const label of path.split("/").filter(Boolean)) {
    current = current ? `${current}/${label}` : label;
    segments.push({ label, path: current });
  }
  return segments;
}

export function ChooseRepositoryStep() {
  const { state, dispatch, roots } = useWizard();
  const source = state.source;
  const mode = source.mode;
  const isLink = mode === "link";
  const isInit = mode === "init";
  const rootId = source.mode === "link" || source.mode === "init" ? source.rootId : null;
  const availableRoots = useMemo(
    () => roots.status === "ready" ? roots.roots.filter((root) => root.readable && (!isInit || root.writable)) : [],
    [roots, isInit],
  );
  const selectedRoot = availableRoots.find((root) => root.id === rootId) ?? null;
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<ProjectRootBrowseEntry[]>([]);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [activePath, setActivePath] = useState<string | null>(null);
  const uid = useId();

  useEffect(() => {
    setPath("");
    setEntries([]);
    setActivePath(null);
  }, [rootId, mode]);

  useEffect(() => {
    if (!isLink || !rootId) return;
    let active = true;
    setLoading(true);
    setBrowseError(null);
    void browseProjectRoot(rootId, path).then(
      (result) => {
        if (!active) return;
        setPath(result.relativePath);
        setEntries(result.entries);
        setActivePath(result.entries[0]?.relativePath ?? null);
      },
      (error: unknown) => {
        if (active) setBrowseError(error instanceof Error ? error.message : "Could not load directories.");
      },
    ).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [isLink, rootId, path]);

  if (!isLink && !isInit) return <GithubRepositoryStep />;

  const chooseRoot = (nextRootId: string) => {
    if (isLink) dispatch({ type: "update_source", mode: "link", patch: { rootId: nextRootId, relativePath: null } });
    if (isInit) dispatch({ type: "update_source", mode: "init", patch: { rootId: nextRootId } });
  };
  const selectEntry = (entry: ProjectRootBrowseEntry) => {
    if (entry.selectable) {
      dispatch({ type: "update_source", mode: "link", patch: { relativePath: entry.relativePath } });
      setActivePath(entry.relativePath);
    } else if (entry.isDirectory) {
      setPath(entry.relativePath);
    }
  };
  const activeIndex = Math.max(0, entries.findIndex((entry) => entry.relativePath === activePath));
  const onTreeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!entries.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      const next = (activeIndex + direction + entries.length) % entries.length;
      setActivePath(entries[next]!.relativePath);
    }
    if (event.key === "Enter" || event.key === "ArrowRight") {
      event.preventDefault();
      selectEntry(entries[activeIndex]!);
    }
  };
  const selectedPath = source.mode === "link" ? source.relativePath : null;
  const directoryName = source.mode === "init" ? source.directoryName : "";
  const nameError = isInit ? directoryNameError(directoryName) : null;

  return (
    <div className="space-y-4">
      <div>
        <label htmlFor={`${uid}-root`} className="block text-sm font-medium text-gray-200">Project root</label>
        <select
          id={`${uid}-root`}
          value={rootId ?? ""}
          onChange={(event) => chooseRoot(event.target.value)}
          className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100"
        >
          <option value="" disabled>Choose a project root</option>
          {availableRoots.map((root) => <option key={root.id} value={root.id}>{root.label} — {root.displayPath}</option>)}
        </select>
      </div>

      {isInit && (
        <div>
          <label htmlFor={`${uid}-directory`} className="block text-sm font-medium text-gray-200">New directory name</label>
          <input
            id={`${uid}-directory`}
            value={directoryName}
            onChange={(event) => dispatch({ type: "update_source", mode: "init", patch: { directoryName: event.target.value } })}
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? `${uid}-directory-error` : undefined}
            className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100"
          />
          {nameError && <p id={`${uid}-directory-error`} role="alert" className="mt-1 text-sm text-red-300">{nameError}</p>}
        </div>
      )}

      {isLink && selectedRoot && (
        <div className="space-y-2">
          <nav aria-label="Repository path" className="flex flex-wrap gap-1 text-sm">
            <button type="button" onClick={() => setPath("")} className="text-indigo-300 underline">{selectedRoot.label}</button>
            {breadcrumbSegments(path).map((segment) => (
              <span key={segment.path} className="flex gap-1">
                <span aria-hidden="true">/</span>
                <button type="button" onClick={() => setPath(segment.path)} className="text-indigo-300 underline">{segment.label}</button>
              </span>
            ))}
          </nav>
          {loading && <p className="text-sm text-gray-400">Loading directories…</p>}
          {browseError && <p role="alert" className="text-sm text-red-300">{browseError}</p>}
          {!loading && !browseError && (
            <div
              role="tree"
              aria-label={`${selectedRoot.label} directories`}
              aria-activedescendant={activePath ? `${uid}-entry-${activePath.replace(/[^a-zA-Z0-9_-]/g, "-")}` : undefined}
              tabIndex={0}
              onKeyDown={onTreeKeyDown}
              className="rounded border border-gray-700 outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
            >
              {entries.length === 0 && <p className="p-3 text-sm text-gray-400">No directories found.</p>}
              {entries.map((entry) => {
                const id = `${uid}-entry-${entry.relativePath.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
                return (
                  <div
                    id={id}
                    key={entry.relativePath}
                    role="treeitem"
                    aria-level={path ? path.split("/").length + 1 : 1}
                    aria-selected={selectedPath === entry.relativePath}
                    aria-disabled={!entry.selectable && !entry.isDirectory ? true : undefined}
                    onClick={() => selectEntry(entry)}
                    className={`cursor-pointer px-3 py-2 text-sm outline-none ${selectedPath === entry.relativePath ? "bg-indigo-500/20 text-indigo-100" : "text-gray-200 hover:bg-gray-800"}`}
                  >
                    <span>{entry.name}</span>
                    {entry.isGitRepository && <span className="ml-2 text-xs text-gray-400">Git repository</span>}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
