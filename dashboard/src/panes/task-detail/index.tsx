import { useCallback, useEffect, useState } from "react";
import type { ClipboardEvent, DragEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ArrowTopRightOnSquareIcon,
  ClipboardIcon,
  PhotoIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import {
  useTask,
  useGates,
  useResolveGate,
  useDeleteTask,
  useReopenWithFeedback,
  useTaskAttachments,
  useUploadTaskAttachment,
  useDeleteTaskAttachment,
  type Task,
  type TaskCompletionDetail,
  type TaskRef,
  type GateSummary,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import TaskActions from "../../components/TaskActions";
import TaskComments from "../../components/TaskComments";
import TaskSessions from "../../components/TaskSessions";
import TaskAttention from "../../components/TaskAttention";
import TaskDescription from "../../components/TaskDescription";
import Modal from "../../components/Modal";
import { useShellPaneStore } from "../store";
import type { PaneViewProps } from "../types";
import type { TaskDetailArgs } from "./manifest";

/** One row of the deliverables checklist, verdict included. */
type DeliverableRow = { id: string; kind: string; target: string; met: boolean; reason: string };

type CompletionWithDeliverables = TaskCompletionDetail & {
  deliverables?: DeliverableRow[];
};

type TaskWithLooseFields = Task & {
  intelligence_class?: string;
  branch_name?: string;
  completion?: CompletionWithDeliverables | null;
};

/**
 * An open task's declared deliverables have no verdict yet — the close-time
 * check is what produces one — so they render as unmet with a pending note.
 *
 * The daemon types a declared deliverable as a loose ``dict[str, str]``, which
 * reaches us as an index signature. Spreading one drops its named fields, so
 * the row is rebuilt field by field instead.
 */
function pendingDeliverable(item: Record<string, string>): DeliverableRow {
  return {
    id: item.id ?? "",
    kind: item.kind ?? "",
    target: item.target ?? "",
    met: false,
    reason: "Pending close-time check",
  };
}

type LocalModal = "close" | "reopen" | null;

export default function TaskDetailPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<TaskDetailArgs>) {
  const navigate = useNavigate();
  const { data: task, isLoading, isError } = useTask(args.taskId);
  const { data: gates } = useGates({ projectId: task?.project_id, enabled: !!task?.project_id });
  const resolveGate = useResolveGate();
  const deleteTask = useDeleteTask();
  const reopenWithFeedback = useReopenWithFeedback();
  const attachments = useTaskAttachments(args.taskId, !!task);
  const uploadAttachment = useUploadTaskAttachment();
  const deleteAttachment = useDeleteTaskAttachment();
  const { open, close } = useShellPaneStore();

  const [modal, setModal] = useState<LocalModal>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{ complete: number; total: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? location.pathname + location.search;
  const openFull = useCallback(() => {
    close();
    navigate(`/tasks/${encodeURIComponent(args.taskId)}`, {
      state: { from },
    });
  }, [args.taskId, close, from, navigate]);

  const loose = task as TaskWithLooseFields | undefined;

  const taskGates = (
    (gates ?? []) as Array<GateSummary & { task_ids?: string[] }>
  ).filter((g) => (g.task_ids ?? []).includes(args.taskId));

  const uploadFiles = useCallback(async (files: File[]) => {
    const images = files.filter((file) => file.type.startsWith("image/"));
    if (images.length === 0) return;
    setUploadError(null);
    setUploadProgress({ complete: 0, total: images.length });
    try {
      for (const [index, image] of images.entries()) {
        await uploadAttachment.mutateAsync({ taskId: args.taskId, file: image });
        setUploadProgress({ complete: index + 1, total: images.length });
      }
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Attachment upload failed");
    } finally {
      setUploadProgress(null);
    }
  }, [args.taskId, uploadAttachment]);

  const onPaste = (event: ClipboardEvent<HTMLDivElement>) => {
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (files.length > 0) {
      event.preventDefault();
      void uploadFiles(files);
    }
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    void uploadFiles(Array.from(event.dataTransfer.files));
  };

  // Must stay inside effects: `setToolbar`/`setShortcuts` are ShellPaneHost
  // useState setters, so publishing during render re-renders the parent on
  // every pass and loops forever.
  useEffect(() => {
    setToolbar([
      {
        id: "open-full",
        label: "Open full detail page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: openFull,
      },
      {
        id: "copy-id",
        label: "Copy id",
        icon: ClipboardIcon,
        onClick: () => navigator.clipboard.writeText(args.taskId),
      },
    ]);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openFull]);

  useEffect(() => {
    setShortcuts([
      { key: "o", label: "Open full detail page", onFire: openFull },
      { key: "c", label: "Close task", onFire: () => setModal("close") },
      { key: "r", label: "Reopen with feedback", onFire: () => setModal("reopen") },
      { key: ".", label: "More actions", onFire: () => setMoreOpen(true) },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openFull]);

  if (isError) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 p-8 text-center">
        <p className="text-sm text-gray-400">Task not found.</p>
      </div>
    );
  }

  return (
    <div
      className="relative flex flex-col gap-4 p-4"
      data-testid="task-attachment-drop-zone"
      onPaste={onPaste}
      onDragEnter={(event) => {
        event.preventDefault();
        setDragActive(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDragActive(false);
        }
      }}
      onDrop={onDrop}
    >
      {dragActive && (
        <div className="pointer-events-none absolute inset-2 z-20 flex items-center justify-center rounded-lg border-2 border-dashed border-indigo-400 bg-gray-950/90">
          <p className="flex items-center gap-2 text-sm font-medium text-indigo-200">
            <PhotoIcon className="h-5 w-5" /> Drop screenshots to attach
          </p>
        </div>
      )}
      <header className="min-w-0">
        <p className="truncate font-mono text-xs text-gray-500">{args.taskId}</p>
        <h2 className="mt-0.5 truncate text-lg font-semibold text-gray-100">
          {isLoading && !task ? "Loading…" : (task?.title ?? "Loading…")}
        </h2>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {task?.status && <StatusBadge status={task.status} />}
          {task?.project_id && <span className="text-gray-400">{task.project_id}</span>}
          {task?.priority != null && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              P{task.priority}
            </span>
          )}
          {task?.task_type && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.task_type}
            </span>
          )}
          {task?.profile_id && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.profile_id}
            </span>
          )}
          {loose?.intelligence_class && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-300">
              {loose.intelligence_class}
            </span>
          )}
          {task?.is_plan_subtask && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-400">
              subtask
            </span>
          )}
        </div>
      </header>

      {task && <TaskActions task={task} returnTo={location.pathname + location.search} onDeleted={close} onOpenTerminal={close} />}

      {task && <TaskAttention task={task as Task & { needs_attention?: string | null }} />}

      {task && <TaskDescription key={task.id} task={task} />}

      {task && (
        <section>
          <div className="mb-1.5 flex items-center justify-between gap-3">
            <h3 className="text-xs font-semibold uppercase text-gray-500">Attachments</h3>
            <span className="text-xs text-gray-500">Drop or paste an image</span>
          </div>
          {uploadProgress && (
            <div className="mb-2" role="status">
              <div className="mb-1 flex justify-between text-xs text-gray-400">
                <span>Uploading screenshots…</span>
                <span>{uploadProgress.complete} / {uploadProgress.total}</span>
              </div>
              <progress
                aria-label="Attachment upload progress"
                className="h-1.5 w-full accent-indigo-500"
                max={uploadProgress.total}
                value={uploadProgress.complete}
              />
            </div>
          )}
          {uploadError && <p className="mb-2 text-xs text-red-400" role="alert">{uploadError}</p>}
          {(attachments.data?.attachments ?? []).length > 0 ? (
            <ul className="grid grid-cols-2 gap-2">
              {(attachments.data?.attachments ?? []).map((attachment) => (
                <li key={attachment.id} className="group relative overflow-hidden rounded-lg border border-gray-800 bg-gray-900">
                  <a href={attachment.url} target="_blank" rel="noreferrer">
                    <img
                      src={attachment.url}
                      alt={attachment.name}
                      className="h-28 w-full object-cover"
                    />
                  </a>
                  <div className="flex items-center gap-2 px-2 py-1.5">
                    <span className="min-w-0 flex-1 truncate text-xs text-gray-300" title={attachment.name}>
                      {attachment.name}
                    </span>
                    <button
                      type="button"
                      aria-label={`Remove ${attachment.name}`}
                      disabled={deleteAttachment.isPending}
                      onClick={() => deleteAttachment.mutate({
                        taskId: args.taskId,
                        attachmentId: attachment.id,
                      })}
                      className="rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-red-400 disabled:opacity-50"
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="rounded-lg border border-dashed border-gray-800 px-3 py-4 text-center text-xs text-gray-500">
              Drag screenshots anywhere onto this pane, or paste from the clipboard.
            </div>
          )}
        </section>
      )}

      {task && <TaskSessions taskId={args.taskId} onOpenSession={close} fromTaskPane />}

      {task && <TaskComments taskId={args.taskId} />}

      <section>
        <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Details</h3>
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm sm:grid-cols-2">
          <MetaField label="Agent" value={task?.assigned_agent ?? "—"} />
          <MetaField
            label="Retries"
            value={`${task?.retry_count ?? 0} / ${task?.max_retries ?? 3}`}
          />
          <MetaField
            label="Integration"
            value={
              task?.effective_integration_mode
                ? task.integration_mode_source === "task"
                  ? task.effective_integration_mode
                  : `${task.effective_integration_mode} (${task.integration_mode_source ?? "policy"})`
                : task?.integration_mode ?? "policy"
            }
          />
          <MetaField
            label="Skip verification"
            value={task?.skip_verification ? "Yes" : "No"}
          />
          <MetaField label="Branch" value={loose?.branch_name ?? "—"} mono />
          <MetaField label="Created" value={formatDate(task?.created_at)} />
          <MetaField label="Updated" value={formatDate(task?.updated_at)} />
          {task?.parent_task_id && (
            <div>
              <span className="text-xs text-gray-500">Parent task</span>
              <p className="mt-0.5">
                <button
                  type="button"
                  onClick={() => open("task-detail", { taskId: task.parent_task_id })}
                  className="font-mono text-xs text-indigo-400 hover:underline"
                >
                  {task.parent_task_id}
                </button>
              </p>
            </div>
          )}
        </div>
      </section>

      {task?.pr_url && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Pull request</h3>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-indigo-400 hover:underline"
          >
            {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
          </a>
        </section>
      )}

      {loose?.completion && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Completion</h3>
          <div className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm">
            <div className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
              <MetaField label="Outcome" value={loose.completion.outcome} />
              <MetaField label="Work outcome" value={loose.completion.work_outcome ?? "—"} />
              <MetaField label="Branch" value={loose.completion.branch ?? "—"} mono />
              <MetaField
                label="Completed"
                value={formatDate(loose.completion.completed_at)}
              />
            </div>
            <CompletionText label="Changes" value={loose.completion.changes} />
            <CompletionText label="Verification" value={loose.completion.verification} />
            <CompletionList label="Tests" values={loose.completion.tests} />
            <CompletionList label="Commands" values={loose.completion.commands} />
            <CompletionList label="Commits" values={loose.completion.commits} mono />
            <CompletionText label="Summary" value={loose.completion.summary} />
            <CompletionText label="Notes" value={loose.completion.notes} />
            {(loose.completion.deliverables ?? []).length > 0 && (
              <DeliverablesChecklist items={loose.completion.deliverables ?? []} />
            )}
            {loose.completion.pr_url && (
              <div>
                <p className="text-xs text-gray-500">Pull request</p>
                <a
                  href={loose.completion.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 inline-flex items-center gap-1.5 text-indigo-400 hover:underline"
                >
                  {loose.completion.pr_url}
                  <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
                </a>
              </div>
            )}
          </div>
        </section>
      )}

      {!loose?.completion && (loose?.deliverables ?? []).length > 0 && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Deliverables</h3>
          <DeliverablesChecklist items={(loose?.deliverables ?? []).map(pendingDeliverable)} />
        </section>
      )}

      {taskGates.length > 0 && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Gates</h3>
          <ul className="space-y-1.5">
            {taskGates.map((g) => (
              <li
                key={g.id}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 p-2.5 text-sm"
              >
                <span className="text-gray-300">
                  {g.gate_type} <span className="text-xs text-gray-500">{g.status}</span>
                </span>
                {g.gate_type === "human" && g.status === "open" && (
                  <span className="flex gap-1.5">
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "approve" })
                      }
                      className="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "reject" })
                      }
                      className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-500"
                    >
                      Reject
                    </button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {(task?.subtasks ?? []).length > 0 && (
        <TaskRefSection title="Subtasks" items={task!.subtasks!} onOpen={open} />
      )}
      {(task?.depends_on ?? []).length > 0 && (
        <TaskRefSection title="Depends on" items={task!.depends_on!} onOpen={open} />
      )}
      {(task?.blocks ?? []).length > 0 && (
        <TaskRefSection title="Blocks" items={task!.blocks!} onOpen={open} />
      )}

      <Modal open={modal === "close"} onClose={() => setModal(null)} title="Delete Task">
        <div className="space-y-4">
          <p className="text-sm text-gray-300">
            Are you sure you want to delete <strong>{task?.title}</strong>? This cannot be undone.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setModal(null)}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={() =>
                deleteTask.mutate({ task_id: args.taskId }, { onSuccess: () => setModal(null) })
              }
              disabled={deleteTask.isPending}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
            >
              {deleteTask.isPending ? "Deleting..." : "Delete"}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={modal === "reopen"} onClose={() => setModal(null)} title="Reopen with Feedback">
        <div className="space-y-4">
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="Provide feedback..."
            rows={4}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setModal(null)}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={() =>
                reopenWithFeedback.mutate(
                  { task_id: args.taskId, feedback },
                  { onSuccess: () => setModal(null) },
                )
              }
              disabled={!feedback.trim() || reopenWithFeedback.isPending}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {reopenWithFeedback.isPending ? "Submitting..." : "Submit"}
            </button>
          </div>
        </div>
      </Modal>

      {moreOpen && (
        <div
          role="menu"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => setMoreOpen(false)}
        >
          <ul
            className="min-w-[220px] rounded-lg border border-gray-800 bg-gray-900 p-1.5 text-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  openFull();
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
              >
                Open full detail page
              </button>
            </li>
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  setModal("reopen");
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
              >
                Reopen with feedback
              </button>
            </li>
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  setModal("close");
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-red-400 hover:bg-gray-800"
              >
                Delete
              </button>
            </li>
          </ul>
        </div>
      )}
    </div>
  );
}

function CompletionText({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <p className="mt-0.5 whitespace-pre-wrap text-gray-300">{value}</p>
    </div>
  );
}

function CompletionList({
  label,
  values,
  mono = false,
}: {
  label: string;
  values?: string[];
  mono?: boolean;
}) {
  if (!values?.length) return null;
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <ul className={`mt-0.5 space-y-1 text-gray-300 ${mono ? "font-mono text-xs" : ""}`}>
        {values.map((value) => <li key={value}>{value}</li>)}
      </ul>
    </div>
  );
}

function DeliverablesChecklist({
  items,
}: {
  items: DeliverableRow[];
}) {
  return (
    <div>
      <p className="text-xs text-gray-500">Deliverables</p>
      <ul className="mt-1 space-y-1.5">
        {items.map((item) => (
          <li key={item.id} className="flex gap-2 text-sm">
            <span aria-label={item.met ? "Met" : "Unmet"} className={item.met ? "text-emerald-400" : "text-amber-400"}>
              {item.met ? "☑" : "☐"}
            </span>
            <span className="min-w-0 text-gray-300">
              <span className="font-mono text-xs">{item.id}</span> — {item.kind}: <code>{item.target}</code>
              {item.reason && <span className="block text-xs text-amber-300">{item.reason}</span>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MetaField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-gray-500">{label}</span>
      <p className={`truncate text-gray-300 ${mono ? "font-mono text-xs" : ""}`} title={value}>
        {value}
      </p>
    </div>
  );
}

function TaskRefSection({
  title,
  items,
  onOpen,
}: {
  title: string;
  items: TaskRef[];
  onOpen: (viewId: string, args: unknown) => void;
}) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">{title}</h3>
      <ul className="space-y-1">
        {items.map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-2 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
          >
            <button
              type="button"
              onClick={() => onOpen("task-detail", { taskId: r.id })}
              className="min-w-0 flex-1 truncate text-left text-indigo-400 hover:underline"
              title={r.title}
            >
              {r.title}
            </button>
            <StatusBadge status={r.status} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "—";
  try {
    const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return d.toLocaleString();
  } catch {
    return String(value);
  }
}
