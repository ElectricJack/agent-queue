/**
 * Task attachment endpoints — screenshot upload / list / serve / delete.
 *
 * These live outside the generated @aq/ts-client because the upload is
 * multipart form data and the serve endpoint returns raw binary (per
 * dashboard/CLAUDE.md, legacy-fetch is the right home for routes not
 * usefully modelled by the generated SDK).
 */
import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { legacyFetch } from "./legacy-fetch";

export const ACCEPTED_ATTACHMENT_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
];

export interface TaskAttachmentMeta {
  path: string;
  filename: string;
  content_type: string;
  exists: boolean;
  size: number | null;
}

export interface TaskAttachmentsResponse {
  success: boolean;
  attachments: TaskAttachmentMeta[];
}

/** URL serving the raw attachment bytes — usable directly as an <img> src. */
export function taskAttachmentUrl(taskId: string, path: string): string {
  return (
    `${import.meta.env.VITE_API_URL || ""}/api/tasks/${encodeURIComponent(taskId)}` +
    `/attachment?path=${encodeURIComponent(path)}`
  );
}

export function useTaskAttachments(taskId: string) {
  return useQuery({
    queryKey: ["task", taskId, "attachments"],
    queryFn: async () => {
      const res = await legacyFetch(`/api/tasks/${encodeURIComponent(taskId)}/attachments`);
      if (!res.ok) throw new Error(`attachments ${res.status}`);
      return (await res.json()) as TaskAttachmentsResponse;
    },
    enabled: !!taskId,
  });
}

async function uploadAttachment(taskId: string, file: File): Promise<TaskAttachmentMeta> {
  const form = new FormData();
  form.append("file", file, file.name || "pasted.png");
  const res = await legacyFetch(`/api/tasks/${encodeURIComponent(taskId)}/attachments`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    let detail = `upload failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // keep the status-based message
    }
    throw new Error(detail);
  }
  const body = (await res.json()) as { attachment: TaskAttachmentMeta };
  return body.attachment;
}

export function useUploadTaskAttachment(taskId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadAttachment(taskId, file),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["task", taskId] });
    },
  });
}

export function useDeleteTaskAttachment(taskId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (path: string) => {
      const res = await legacyFetch(
        `/api/tasks/${encodeURIComponent(taskId)}/attachment?path=${encodeURIComponent(path)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`delete failed (${res.status})`);
      return (await res.json()) as { success: boolean; attachments: string[] };
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["task", taskId] });
    },
  });
}

/**
 * Shared upload entry point for drag-drop and clipboard paste.
 *
 * Filters to the accepted content types, uploads sequentially (uploads are
 * small; sequential keeps error attribution simple), and surfaces the last
 * error plus an in-flight count for progress UI.
 */
export function useAttachmentUploader(taskId: string) {
  const upload = useUploadTaskAttachment(taskId);
  const [pending, setPending] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const uploadFiles = useCallback(
    async (files: Iterable<File>) => {
      const accepted = [...files].filter((f) => ACCEPTED_ATTACHMENT_TYPES.includes(f.type));
      if (accepted.length === 0) return;
      setError(null);
      setPending((n) => n + accepted.length);
      for (const file of accepted) {
        try {
          await upload.mutateAsync(file);
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setPending((n) => n - 1);
        }
      }
    },
    [upload],
  );

  return { uploadFiles, pending, error, clearError: () => setError(null) };
}
