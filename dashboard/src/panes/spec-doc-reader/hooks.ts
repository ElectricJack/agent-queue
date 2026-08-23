import { useMemo } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { legacyFetch } from "../../api/legacy-fetch";
import {
  extractToc,
  parseFrontmatter,
  resolveTitle,
  stripLeadingH1,
  type TocEntry,
} from "./docProcessing";
import type { SpecDocReaderArgs } from "./manifest";

export interface WorkspaceFileResponse {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
}

const HOSTED_DOC_CAP_BYTES = 500 * 1024;

export class HostedDocFetchError extends Error {
  status: number;
  url: string;
  constructor(status: number, url: string) {
    super(`hosted doc fetch failed: ${status} ${url}`);
    this.status = status;
    this.url = url;
  }
}

export class WorkspaceFileFetchError extends Error {
  status: number;
  path: string;
  constructor(status: number, path: string) {
    super(`workspace file fetch failed: ${status} ${path}`);
    this.status = status;
    this.path = path;
  }
}

// GET /api/workspaces/{id}/file returns raw text/plain content (or, for
// binary files, a JSON {reason: "binary", size, path} placeholder) — not
// the {path, content, size, truncated} JSON envelope the design doc's
// original draft assumed. This mirrors dashboard/src/api/taskFiles.ts's
// `fetchTaskFileText`, the established pattern for this same
// serve_workspace_relative_file-backed contract (src/api/file_serving.py).
export function useWorkspaceFile(workspaceId: string, path: string, enabled: boolean) {
  return useQuery({
    queryKey: ["workspace-file", workspaceId, path],
    queryFn: async (): Promise<WorkspaceFileResponse> => {
      const res = await legacyFetch(
        `/api/workspaces/${encodeURIComponent(workspaceId)}/file?path=${encodeURIComponent(path)}`,
      );
      if (!res.ok) throw new WorkspaceFileFetchError(res.status, path);

      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        const body = (await res.json()) as { reason?: string; size?: number };
        if (body.reason === "binary") {
          const kb = body.size ? ` (${Math.round(body.size / 1024)} KB)` : "";
          return {
            path,
            content: `(binary file omitted${kb})`,
            size: body.size ?? 0,
            truncated: false,
          };
        }
      }
      const content = await res.text();
      return { path, content, size: content.length, truncated: false };
    },
    enabled,
    staleTime: 30_000,
  });
}

// Deliberate exception to "never call fetch directly for daemon endpoints"
// (dashboard/CLAUDE.md): this isn't a daemon command, it's an arbitrary
// same-origin doc route with no SDK binding — see spec §7.2.
export function useHostedDoc(url: string, enabled: boolean) {
  return useQuery({
    queryKey: ["hosted-doc", url],
    queryFn: async (): Promise<WorkspaceFileResponse> => {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new HostedDocFetchError(res.status, url);
      const full = await res.text();
      const truncated = full.length > HOSTED_DOC_CAP_BYTES;
      const content = truncated ? full.slice(0, HOSTED_DOC_CAP_BYTES) : full;
      return { path: url, content, size: full.length, truncated };
    },
    enabled,
    staleTime: 30_000,
  });
}

const MARKDOWN_EXT = /\.(md|markdown)$/i;
const HEADING_LINE = /^#{1,6}\s/m;

function looksLikeMarkdown(pathOrUrl: string, content: string): boolean {
  if (MARKDOWN_EXT.test(pathOrUrl)) return true;
  return HEADING_LINE.test(content.slice(0, 2048));
}

export interface ProcessedDoc {
  isMarkdown: boolean;
  frontmatter: Record<string, unknown> | null;
  body: string;
  title: string;
  toc: TocEntry[];
  truncated: boolean;
  rawContent: string;
}

/**
 * Shared post-processing pipeline: whichever data hook resolved
 * (local-file or url), run frontmatter split → markdown sniff → TOC
 * extraction → title resolution once via useMemo.
 */
export function useProcessedDoc(
  args: SpecDocReaderArgs,
  fileQuery: UseQueryResult<WorkspaceFileResponse>,
): ProcessedDoc | null {
  const data = fileQuery.data;
  return useMemo(() => {
    if (!data) return null;
    const pathOrUrl = args.path ?? args.url ?? data.path;
    const isMarkdown = looksLikeMarkdown(pathOrUrl, data.content);

    if (!isMarkdown) {
      return {
        isMarkdown: false,
        frontmatter: null,
        body: data.content,
        title: resolveTitle({ frontmatter: null, body: "", fallbackName: pathOrUrl }),
        toc: [],
        truncated: data.truncated,
        rawContent: data.content,
      };
    }

    const { data: frontmatter, content: body } = parseFrontmatter(data.content);
    const title = resolveTitle({ frontmatter, body, fallbackName: pathOrUrl });
    // extractToc runs on the unstripped body so its slugger consumes the
    // leading h1 in document order (slug-parity with rehype-slug on the
    // full source); the rendered body has that same h1 stripped so the
    // resolved title (rendered separately, above the meta card) isn't
    // duplicated inside the markdown body itself.
    const toc = extractToc(body);
    const renderBody = stripLeadingH1(body);

    return {
      isMarkdown: true,
      frontmatter,
      body: renderBody,
      title,
      toc,
      truncated: data.truncated,
      rawContent: data.content,
    };
  }, [data, args.path, args.url]);
}
