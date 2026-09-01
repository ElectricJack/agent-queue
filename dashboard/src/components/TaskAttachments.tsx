import { useEffect, useRef } from "react";
import {
  ArrowUpTrayIcon,
  DocumentIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import {
  taskAttachmentUrl,
  useAttachmentUploader,
  useDeleteTaskAttachment,
  useTaskAttachments,
  ACCEPTED_ATTACHMENT_TYPES,
  type TaskAttachmentMeta,
} from "../api/taskAttachments";

interface Props {
  taskId: string;
  /**
   * Shared uploader instance (e.g. the pane's drag-drop overlay). When
   * omitted the component creates its own.
   */
  uploader?: ReturnType<typeof useAttachmentUploader>;
  /**
   * Register a document-level paste listener so a screenshot pasted
   * anywhere in the view uploads here. Leave off when another surface for
   * a different task may be mounted at the same time.
   */
  capturePaste?: boolean;
}

export default function TaskAttachments({ taskId, uploader, capturePaste = false }: Props) {
  const own = useAttachmentUploader(taskId);
  const { uploadFiles, pending, error, clearError } = uploader ?? own;
  const { data } = useTaskAttachments(taskId);
  const deleteAttachment = useDeleteTaskAttachment(taskId);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!capturePaste) return;
    const onPaste = (e: ClipboardEvent) => {
      const files = [...(e.clipboardData?.files ?? [])];
      if (files.length === 0) return;
      e.preventDefault();
      void uploadFiles(files);
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [capturePaste, uploadFiles]);

  const attachments = data?.attachments ?? [];

  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Attachments</h3>

      {attachments.length > 0 && (
        <ul className="mb-2 grid grid-cols-3 gap-2 sm:grid-cols-4">
          {attachments.map((a) => (
            <AttachmentTile
              key={a.path}
              taskId={taskId}
              meta={a}
              onRemove={() => deleteAttachment.mutate(a.path)}
              removing={deleteAttachment.isPending && deleteAttachment.variables === a.path}
            />
          ))}
        </ul>
      )}

      <button
        type="button"
        data-testid="attachment-dropzone"
        onClick={() => fileInput.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDrop={(e) => {
          e.preventDefault();
          void uploadFiles(e.dataTransfer.files);
        }}
        className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-gray-700 bg-gray-900/50 px-3 py-3 text-xs text-gray-500 hover:border-gray-500 hover:text-gray-400"
      >
        <ArrowUpTrayIcon className="h-4 w-4" />
        {pending > 0
          ? `Uploading ${pending} file${pending === 1 ? "" : "s"}…`
          : "Drop a screenshot, paste from clipboard, or click to browse"}
      </button>
      <input
        ref={fileInput}
        type="file"
        multiple
        accept={ACCEPTED_ATTACHMENT_TYPES.join(",")}
        className="hidden"
        onChange={(e) => {
          if (e.target.files) void uploadFiles(e.target.files);
          e.target.value = "";
        }}
      />

      {error && (
        <p className="mt-1.5 flex items-center gap-2 text-xs text-red-400">
          {error}
          <button type="button" onClick={clearError} className="text-gray-500 hover:text-gray-300">
            dismiss
          </button>
        </p>
      )}
    </section>
  );
}

function AttachmentTile({
  taskId,
  meta,
  onRemove,
  removing,
}: {
  taskId: string;
  meta: TaskAttachmentMeta;
  onRemove: () => void;
  removing: boolean;
}) {
  const isImage = meta.content_type.startsWith("image/") && meta.exists;
  const url = taskAttachmentUrl(taskId, meta.path);
  return (
    <li className="group relative overflow-hidden rounded-lg border border-gray-800 bg-gray-900">
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={meta.path}
        className="block"
      >
        {isImage ? (
          <img
            src={url}
            alt={meta.filename}
            loading="lazy"
            className="h-20 w-full object-cover"
          />
        ) : (
          <span className="flex h-20 w-full flex-col items-center justify-center gap-1 px-1 text-gray-500">
            <DocumentIcon className="h-6 w-6" />
            <span className="max-w-full truncate text-[10px]">
              {meta.filename}
              {!meta.exists && " (missing)"}
            </span>
          </span>
        )}
      </a>
      <button
        type="button"
        aria-label={`Remove ${meta.filename}`}
        onClick={onRemove}
        disabled={removing}
        className="absolute right-1 top-1 hidden rounded-full bg-black/70 p-0.5 text-gray-300 hover:text-white disabled:opacity-50 group-hover:block"
      >
        <XMarkIcon className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}
