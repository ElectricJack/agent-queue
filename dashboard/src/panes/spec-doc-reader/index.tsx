import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { PaneViewProps } from "../types";
import type { SpecDocReaderArgs } from "./manifest";
import type { TocEntry } from "./docProcessing";
import { useWorkspaceFile, useHostedDoc, useProcessedDoc } from "./hooks";
import MarkdownPreview from "../../components/MarkdownPreview";

const TOC_BREAKPOINT_PX = 420;

function useContainerWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (typeof w === "number") setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

function TocList({
  toc,
  activeId,
  onSelect,
  tocRef,
}: {
  toc: TocEntry[];
  activeId: string | null;
  onSelect: (id: string) => void;
  tocRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <nav ref={tocRef} aria-label="Table of contents" className="text-xs">
      <ul className="space-y-0.5">
        {toc.map((entry) => (
          <li key={entry.id} className={entry.depth === 3 ? "pl-3" : ""}>
            <button
              type="button"
              data-toc-id={entry.id}
              onClick={() => onSelect(entry.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  onSelect(entry.id);
                  (document.querySelector("[data-spec-doc-body]") as HTMLElement | null)?.focus();
                }
              }}
              className={
                "block w-full truncate rounded px-2 py-1 text-left hover:bg-gray-800 " +
                (activeId === entry.id ? "bg-indigo-950/60 text-indigo-200" : "text-gray-400")
              }
            >
              {entry.text}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

const KNOWN_KEYS = new Set(["status", "date", "companion_specs", "companions"]);

function companionList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string")
    return value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  return [];
}

export function MetaCard({
  data,
  onCompanionClick,
}: {
  data: Record<string, unknown> | null;
  onCompanionClick: (companion: string) => void;
}) {
  if (!data || Object.keys(data).length === 0) return null;
  const companions = companionList(data.companion_specs ?? data.companions);
  const unknownEntries = Object.entries(data).filter(([k]) => !KNOWN_KEYS.has(k));

  return (
    <div className="mb-4 rounded border border-gray-800 bg-gray-950 p-3 text-xs">
      {typeof data.status === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Status:</span>
          <span className="rounded-full bg-indigo-950/60 px-2 py-0.5 text-indigo-200">
            {data.status}
          </span>
        </div>
      )}
      {typeof data.date === "string" && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500">Date:</span>
          <span className="text-gray-300">{data.date}</span>
        </div>
      )}
      {companions.length > 0 && (
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <span className="text-gray-500">Companions:</span>
          {companions.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => onCompanionClick(c)}
              className="text-indigo-300 underline hover:text-indigo-200"
            >
              {c}
            </button>
          ))}
        </div>
      )}
      {unknownEntries.map(([key, value]) => (
        <div key={key} className="mb-1.5 flex items-center gap-2">
          <span className="text-gray-500 after:content-[':']">{key}</span>
          <span className="text-gray-300">{String(value)}</span>
        </div>
      ))}
    </div>
  );
}

function siblingPath(currentPath: string, companionFile: string): string {
  const idx = currentPath.lastIndexOf("/");
  const dir = idx === -1 ? "" : currentPath.slice(0, idx + 1);
  return dir + companionFile;
}

export default function SpecDocReaderPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SpecDocReaderArgs>) {
  const isLocalFile = args.workspaceId !== undefined && args.path !== undefined;

  const localQuery = useWorkspaceFile(args.workspaceId ?? "", args.path ?? "", isLocalFile);
  const hostedQuery = useHostedDoc(args.url ?? "", !isLocalFile);
  const fileQuery = isLocalFile ? localQuery : hostedQuery;

  const doc = useProcessedDoc(args, fileQuery);

  const bodyRef = useRef<HTMLDivElement>(null);
  const tocRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const width = useContainerWidth(containerRef);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const navigate = useNavigate();

  const toc = useMemo(() => doc?.toc ?? [], [doc]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    const scrollEl = bodyRef.current;
    if (!scrollEl || toc.length === 0 || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting);
        if (visible.length > 0) {
          const topMost = visible.reduce((a, b) =>
            a.boundingClientRect.top < b.boundingClientRect.top ? a : b,
          );
          setActiveId(topMost.target.id);
        }
      },
      { root: scrollEl, threshold: 0.1 },
    );
    for (const entry of toc) {
      const el = scrollEl.querySelector(`#${CSS.escape(entry.id)}`);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [toc]);

  const scrollToHeading = useCallback((id: string) => {
    const el = bodyRef.current?.querySelector(`#${CSS.escape(id)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setActiveId(id);
  }, []);

  const handleCompanionClick = useCallback(
    (companion: string) => {
      if (isLocalFile && args.path) {
        setArgs({
          workspaceId: args.workspaceId,
          path: siblingPath(args.path, companion),
        } as SpecDocReaderArgs);
      }
      // url mode: no cross-directory companion resolution yet.
    },
    [isLocalFile, args.path, args.workspaceId, setArgs],
  );

  const copyLabel = isLocalFile ? "Copy path" : "Copy URL";
  const copyValue = isLocalFile ? (args.path ?? "") : (args.url ?? "");

  const copyToClipboard = useCallback((value: string, toastText?: string) => {
    void navigator.clipboard.writeText(value);
    if (toastText) setToast(toastText);
  }, []);

  const fullPageRoute = useMemo(() => {
    const pathOrUrl = args.path ?? args.url ?? "";
    if (/vault\/.*\/playbooks\/[^/]+\.md$/.test(pathOrUrl)) {
      const playbookId = pathOrUrl.split("/").pop()!.replace(/\.md$/, "");
      return `/settings/playbooks/${playbookId}`;
    }
    const fmPlaybookId = doc?.frontmatter?.playbook_id;
    if (typeof fmPlaybookId === "string" && fmPlaybookId) {
      return `/settings/playbooks/${fmPlaybookId}`;
    }
    return null;
  }, [args.path, args.url, doc?.frontmatter]);

  useEffect(() => {
    // Copy path/url stays available through error states (404/403/etc) so
    // the user can still grab the reference even when the doc failed to
    // load; only a genuinely pending fetch (no path/url resolved yet)
    // clears the toolbar. Open-in-editor / open-full-page need the doc to
    // have actually loaded (isLocalFile / fullPageRoute otherwise dangle).
    if (fileQuery.isLoading || !copyValue) {
      setToolbar([]);
      return;
    }
    const actions = [
      {
        id: "copy-path-or-url",
        label: copyLabel,
        onClick: () => copyToClipboard(copyValue),
      },
      ...(doc && isLocalFile
        ? [
            {
              id: "open-in-editor",
              label: "Open in editor",
              onClick: () => copyToClipboard(copyValue, "Path copied — open in your editor."),
            },
          ]
        : []),
      ...(doc && fullPageRoute
        ? [
            {
              id: "open-full-page",
              label: "Open full-page view",
              onClick: () => navigate(fullPageRoute),
            },
          ]
        : []),
    ];
    setToolbar(actions);
    return () => setToolbar([]);
  }, [
    doc,
    fileQuery.isLoading,
    isLocalFile,
    copyLabel,
    copyValue,
    fullPageRoute,
    copyToClipboard,
    setToolbar,
    navigate,
  ]);

  useEffect(() => {
    if (!doc) {
      setShortcuts([]);
      return;
    }
    const scrollByLine = (dir: 1 | -1) => {
      bodyRef.current?.scrollBy({ top: dir * 24 });
    };
    const scrollByPage = (dir: 1 | -1) => {
      const el = bodyRef.current;
      if (!el) return;
      el.scrollBy({ top: dir * el.clientHeight });
    };
    const focusToc = () => {
      const targetId = activeId ?? toc[0]?.id;
      const el = targetId
        ? tocRef.current?.querySelector<HTMLButtonElement>(`[data-toc-id="${targetId}"]`)
        : null;
      el?.focus();
    };

    const bindings = [
      { key: "ArrowUp", label: "Scroll up", onFire: () => scrollByLine(-1) },
      { key: "ArrowDown", label: "Scroll down", onFire: () => scrollByLine(1) },
      { key: "k", label: "Scroll up", onFire: () => scrollByLine(-1) },
      { key: "j", label: "Scroll down", onFire: () => scrollByLine(1) },
      { key: "PageUp", label: "Page up", onFire: () => scrollByPage(-1) },
      { key: "PageDown", label: "Page down", onFire: () => scrollByPage(1) },
      { key: "t", label: "Focus table of contents", onFire: focusToc },
    ];
    setShortcuts(bindings);
    return () => setShortcuts([]);
  }, [doc, toc, activeId, setShortcuts]);

  const tocElement = toc.length > 0 && (
    <TocList toc={toc} activeId={activeId} onSelect={scrollToHeading} tocRef={tocRef} />
  );

  if (fileQuery.isLoading) {
    return (
      <div className="p-4" data-testid="spec-doc-reader-skeleton">
        <div className="mb-2 h-5 w-2/3 animate-pulse rounded bg-gray-800" />
        <div className="mb-4 h-16 w-full animate-pulse rounded bg-gray-900" />
        {[...Array(5)].map((_, i) => (
          <div key={i} className="mb-2 h-3 w-full animate-pulse rounded bg-gray-900" />
        ))}
      </div>
    );
  }

  if (fileQuery.isError) {
    const err = fileQuery.error as { status?: number } | undefined;
    const pathOrUrl = args.path ?? args.url ?? "";
    const status = err?.status;
    if (status === 404) {
      return (
        <div className="p-4 text-sm text-gray-400">
          Spec not found at <code>{pathOrUrl}</code>
        </div>
      );
    }
    if (status === 403) {
      return (
        <div className="p-4 text-sm text-red-400">You don&apos;t have access to this workspace.</div>
      );
    }
    if (status === 413) {
      return (
        <div className="p-4 text-sm text-gray-400">
          This document is too large to preview here. Copy the path and open it locally.
        </div>
      );
    }
    return (
      <div className="p-4 text-sm text-red-400">
        Couldn&apos;t load this document.
        <button
          type="button"
          onClick={() => fileQuery.refetch()}
          className="ml-2 rounded border border-gray-700 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-800"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!doc) return null;

  if (!doc.isMarkdown) {
    return (
      <pre className="h-full overflow-auto whitespace-pre-wrap p-4 font-mono text-xs text-gray-200">
        {doc.rawContent}
      </pre>
    );
  }

  if (doc.body.trim().length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-gray-500">
        This document is empty.
      </div>
    );
  }

  return (
    <div ref={containerRef} className="flex h-full flex-col overflow-hidden">
      {toast && (
        <div className="border-b border-gray-800 bg-gray-900 px-4 py-1.5 text-xs text-gray-300">
          {toast}
        </div>
      )}
      <div className="border-b border-gray-800 p-4">
        <h1 className="mb-2 text-lg font-semibold text-gray-100">{doc.title}</h1>
        <MetaCard data={doc.frontmatter} onCompanionClick={handleCompanionClick} />
      </div>
      {doc.truncated && (
        <div className="border-b border-amber-900 bg-amber-950/40 px-4 py-2 text-xs text-amber-300">
          Document truncated — showing first {Math.round((fileQuery.data?.size ?? 0) / 1024)} KB.
          Copy path/url to view the rest locally.
        </div>
      )}
      {width > 0 && width < TOC_BREAKPOINT_PX ? (
        <>
          {tocElement && (
            <details className="border-b border-gray-800 px-3 py-2">
              <summary className="cursor-pointer text-xs text-gray-400">Table of contents</summary>
              <div className="mt-2">{tocElement}</div>
            </details>
          )}
          <div ref={bodyRef} data-spec-doc-body tabIndex={-1} className="flex-1 overflow-y-auto p-4">
            <MarkdownPreview source={doc.body} />
          </div>
        </>
      ) : (
        <div className="flex flex-1 overflow-hidden">
          {tocElement && (
            <aside className="w-[180px] shrink-0 overflow-y-auto border-r border-gray-800 p-2">
              {tocElement}
            </aside>
          )}
          <div ref={bodyRef} data-spec-doc-body tabIndex={-1} className="flex-1 overflow-y-auto p-4">
            <MarkdownPreview source={doc.body} />
          </div>
        </div>
      )}
    </div>
  );
}
