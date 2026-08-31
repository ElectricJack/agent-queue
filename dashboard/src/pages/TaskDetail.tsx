import { useRef, useState } from "react";
import { useParams, Link, useLocation } from "react-router-dom";
import {
  ArrowLeftIcon,
  ArrowTopRightOnSquareIcon,
  ExclamationTriangleIcon,
  PencilIcon,
} from "@heroicons/react/24/outline";
import {
  useEditTask,
  useProjectProfiles,
  useTask,
  type TaskRef,
} from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import TaskActions from "../components/TaskActions";
import TaskComments from "../components/TaskComments";
import TaskSessions from "../components/TaskSessions";
import TaskAttention from "../components/TaskAttention";
import TaskDescription from "../components/TaskDescription";
import TaskGraph, { TaskExplain } from "./task/TaskGraph";
import { workspaceHref } from "../shell/projectNavigation";

interface FormState {
  title: string;
  status: string;
  priority: string;
  task_type: string;
  profile_id: string;
  max_retries: string;
  auto_approve_plan: boolean;
  skip_verification: boolean;
}

const STATUS_OPTIONS = [
  "PENDING",
  "READY",
  "IN_PROGRESS",
  "AWAITING_APPROVAL",
  "AWAITING_PLAN_APPROVAL",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "BLOCKED",
  "CANCELED",
];

interface TaskLike {
  title?: string;
  description?: string;
  status?: string;
  priority?: number;
  task_type?: string | null;
  profile_id?: string | null;
  max_retries?: number;
  auto_approve_plan?: boolean;
  skip_verification?: boolean;
}

function taskToForm(t: TaskLike | null | undefined): FormState {
  return {
    title: t?.title ?? "",
    status: t?.status ?? "",
    priority: t?.priority != null ? String(t.priority) : "",
    task_type: t?.task_type ?? "",
    profile_id: t?.profile_id ?? "",
    max_retries: t?.max_retries != null ? String(t.max_retries) : "",
    auto_approve_plan: !!t?.auto_approve_plan,
    skip_verification: !!t?.skip_verification,
  };
}

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  return <TaskDetailContent key={taskId} taskId={taskId ?? ""} />;
}

function TaskDetailContent({ taskId }: { taskId: string }) {
  const location = useLocation();
  const { data: task, isLoading } = useTask(taskId ?? "");
  const { data: profiles } = useProjectProfiles(task?.project_id ?? "");
  const editTask = useEditTask();

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(taskToForm(task));
  const baseline = useRef<FormState>(taskToForm(task));
  const [fatal, setFatal] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"details" | "explain" | "graph">("details");

  if (isLoading) return <p className="p-6 text-sm text-gray-500">Loading...</p>;
  if (!task) return <p className="p-6 text-sm text-gray-500">Task not found.</p>;

  const from = (location.state as { from?: string } | null)?.from ?? workspaceHref(task.project_id, "tasks");
  const backLabel = labelForBack(from);
  const agent = task.assigned_agent;
  const profileOptions = (profiles?.agent_types ?? [])
    .flatMap((row) => [row.scoped, row.global].filter(Boolean) as { id: string; name: string }[])
    .filter((p, i, arr) => arr.findIndex((q) => q.id === p.id) === i);

  const startEdit = () => {
    baseline.current = taskToForm(task);
    setForm(baseline.current);
    setFatal(null);
    setEditing(true);
  };

  const cancel = () => {
    setForm(taskToForm(task));
    setFatal(null);
    setEditing(false);
  };

  const save = async () => {
    setFatal(null);
    try {
      const body: Record<string, unknown> = { task_id: task.id };
      if (form.title !== baseline.current.title) body.title = form.title;
      if (form.status && form.status !== baseline.current.status && task.status !== "PAUSED") body.status = form.status;
      const priorityNum = parseOptionalInt(form.priority);
      if (priorityNum !== parseOptionalInt(baseline.current.priority)) body.priority = priorityNum;
      if (form.task_type !== baseline.current.task_type) body.task_type = form.task_type || null;
      if (form.profile_id !== baseline.current.profile_id)
        body.profile_id = form.profile_id || null;
      const retriesNum = parseOptionalInt(form.max_retries);
      if (retriesNum !== parseOptionalInt(baseline.current.max_retries)) body.max_retries = retriesNum;
      if (form.auto_approve_plan !== baseline.current.auto_approve_plan)
        body.auto_approve_plan = form.auto_approve_plan;
      if (form.skip_verification !== baseline.current.skip_verification)
        body.skip_verification = form.skip_verification;

      // No fields changed beyond task_id? Skip.
      if (Object.keys(body).length === 1) {
        setEditing(false);
        return;
      }
      await editTask.mutateAsync(body as Parameters<typeof editTask.mutateAsync>[0]);
      setEditing(false);
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      <Link
        to={from}
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200"
      >
        <ArrowLeftIcon className="h-4 w-4" /> {backLabel}
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          {editing ? (
            <input
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-2xl font-bold text-gray-100 focus:border-indigo-500 focus:outline-none"
            />
          ) : (
            <h1 className="text-2xl font-bold">{task.title}</h1>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <StatusBadge status={task.status} />
            {task.project_id && (
              <Link to={workspaceHref(task.project_id, "tasks")} className="text-sm text-indigo-400 hover:underline">{task.project_id}</Link>
            )}
            {task.priority != null && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">
                P{task.priority}
              </span>
            )}
            {task.task_type && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">
                {task.task_type}
              </span>
            )}
            {task.is_plan_subtask && (
              <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-xs text-indigo-400">
                subtask
              </span>
            )}
          </div>
        </div>
        {!editing && (
          <button
            type="button"
            onClick={startEdit}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
          >
            <PencilIcon className="h-4 w-4" /> Edit
          </button>
        )}
      </div>

      {/* Actions */}
      {!editing && <TaskActions task={task} />}

      <TaskAttention task={task as typeof task & { needs_attention?: string | null }} />

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-800">
        {(
          [
            { id: "details", label: "Details" },
            { id: "explain", label: "Explain" },
            { id: "graph", label: "Graph" },
          ] as const
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === t.id
                ? "border-b-2 border-indigo-400 text-indigo-400"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === "explain" && taskId && <TaskExplain taskId={taskId} />}
      {activeTab === "graph" && taskId && <TaskGraph taskId={taskId} from={from} />}

      {activeTab === "details" && (
        <>
      <TaskDescription key={task.id} task={task} />

      <TaskSessions taskId={task.id} />

      <TaskComments taskId={task.id} />

      {/* Metadata grid */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">Details</h2>
        <div className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm sm:grid-cols-2">
          <ReadField label="Agent" value={agent ?? "-"} />
          <EditableSelect
            label="Status"
            editing={editing && task.status !== "PAUSED"}
            value={editing ? form.status : task.status ?? "-"}
            displayValue={task.status ?? "-"}
            options={STATUS_OPTIONS}
            onChange={(v) => setForm({ ...form, status: v })}
            hint="Admin override — bypasses the state machine."
          />
          <EditableInput
            label="Priority"
            type="number"
            editing={editing}
            value={form.priority}
            displayValue={task.priority != null ? String(task.priority) : "-"}
            onChange={(v) => setForm({ ...form, priority: v })}
          />
          <EditableSelect
            label="Profile"
            editing={editing}
            value={form.profile_id}
            displayValue={task.profile_id ?? "default"}
            options={["", ...profileOptions.map((p) => p.id)]}
            optionLabel={(v) =>
              v === "" ? "— inherit / none —" : profileOptions.find((p) => p.id === v)?.name ?? v
            }
            onChange={(v) => setForm({ ...form, profile_id: v })}
          />
          <EditableInput
            label="Task type"
            type="text"
            editing={editing}
            value={form.task_type}
            displayValue={task.task_type ?? "-"}
            onChange={(v) => setForm({ ...form, task_type: v })}
          />
          <EditableInput
            label="Max retries"
            type="number"
            editing={editing}
            value={form.max_retries}
            displayValue={String(task.max_retries ?? "-")}
            onChange={(v) => setForm({ ...form, max_retries: v })}
          />
          <ReadField
            label="Retries used"
            value={`${task.retry_count ?? 0} / ${task.max_retries ?? 3}`}
          />
          <ReadField label="Requires Approval" value={task.requires_approval ? "Yes" : "No"} />
          <EditableCheckbox
            label="Auto-approve Plan"
            editing={editing}
            checked={form.auto_approve_plan}
            displayValue={task.auto_approve_plan ? "Yes" : "No"}
            onChange={(v) => setForm({ ...form, auto_approve_plan: v })}
          />
          <EditableCheckbox
            label="Skip verification"
            editing={editing}
            checked={form.skip_verification}
            displayValue={task.skip_verification ? "Yes" : "No"}
            onChange={(v) => setForm({ ...form, skip_verification: v })}
          />
          <ReadField label="Created" value={formatDate(task.created_at)} />
          <ReadField label="Updated" value={formatDate(task.updated_at)} />
          {task.parent_task_id && (
            <div>
              <span className="text-gray-500">Parent Task</span>
              <p>
                <Link
                  to={`/tasks/${encodeURIComponent(task.parent_task_id)}`}
                  state={{ from }}
                  className="text-indigo-400 hover:underline"
                >
                  {task.parent_task_id}
                </Link>
              </p>
            </div>
          )}
        </div>
      </section>

      {editing && (
        <div className="space-y-3">
          {fatal && (
            <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{fatal}</span>
            </div>
          )}
          <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
            <button
              type="button"
              onClick={cancel}
              className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={editTask.isPending}
              className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            >
              {editTask.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}

      {/* PR link */}
      {task.pr_url && (
        <section>
          <h2 className="mb-2 text-sm font-semibold uppercase text-gray-500">Pull Request</h2>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-indigo-400 hover:underline"
          >
            {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
          </a>
        </section>
      )}

      {/* Subtasks */}
      {task.subtasks && task.subtasks.length > 0 && (
        <TaskRefList title="Subtasks" items={task.subtasks} from={from} />
      )}

      {/* Dependencies */}
      {task.depends_on && task.depends_on.length > 0 && (
        <TaskRefList title="Depends On" items={task.depends_on} from={from} />
      )}

      {/* Blocks */}
      {task.blocks && task.blocks.length > 0 && (
        <TaskRefList title="Blocks" items={task.blocks} from={from} />
      )}
        </>
      )}
    </div>
  );
}

/**
 * Pick a back-button label that matches where the user came from.
 * Falls back to "Back" when the source path is unfamiliar.
 */
function labelForBack(from: string): string {
  if (from.match(/^\/(projects\/[^/]+|command-center)\/tasks/)) return "Back to tasks";
  if (from.match(/^\/projects\/[^/]+\/?$/)) return "Back to project";
  if (from.startsWith("/tasks/")) return "Back";
  return "Back";
}

function ReadField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      <p className="text-gray-300">{value}</p>
    </div>
  );
}

function EditableInput({
  label,
  type,
  editing,
  value,
  displayValue,
  onChange,
}: {
  label: string;
  type: "text" | "number";
  editing: boolean;
  value: string;
  displayValue: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      {editing ? (
        <input
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-0.5 w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
      ) : (
        <p className="text-gray-300">{displayValue}</p>
      )}
    </div>
  );
}

function EditableSelect({
  label,
  editing,
  value,
  displayValue,
  options,
  optionLabel,
  onChange,
  hint,
}: {
  label: string;
  editing: boolean;
  value: string;
  displayValue: string;
  options: string[];
  optionLabel?: (v: string) => string;
  onChange: (v: string) => void;
  hint?: string;
}) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      {editing ? (
        <>
          <select
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className="mt-0.5 w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            {options.map((opt) => (
              <option key={opt} value={opt}>
                {optionLabel ? optionLabel(opt) : opt || "—"}
              </option>
            ))}
          </select>
          {hint && <p className="mt-1 text-xs text-gray-500">{hint}</p>}
        </>
      ) : (
        <p className="text-gray-300">{displayValue}</p>
      )}
    </div>
  );
}

function EditableCheckbox({
  label,
  editing,
  checked,
  displayValue,
  onChange,
}: {
  label: string;
  editing: boolean;
  checked: boolean;
  displayValue: string;
  onChange: (v: boolean) => void;
}) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      {editing ? (
        <p className="mt-0.5">
          <label className="inline-flex items-center gap-2 text-sm text-gray-200">
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => onChange(e.target.checked)}
              className="h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-indigo-500"
            />
            <span>{checked ? "Yes" : "No"}</span>
          </label>
        </p>
      ) : (
        <p className="text-gray-300">{displayValue}</p>
      )}
    </div>
  );
}

function TaskRefList({ title, items, from }: { title: string; items: TaskRef[]; from: string }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold uppercase text-gray-500">{title}</h2>
      <div className="space-y-1">
        {items.map((ref) => (
          <div
            key={ref.id}
            className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-2"
          >
            <Link
              to={`/tasks/${encodeURIComponent(ref.id)}`}
              state={{ from }}
              className="truncate text-sm font-medium text-indigo-400 hover:underline"
            >
              {ref.title}
            </Link>
            <StatusBadge status={ref.status} />
          </div>
        ))}
      </div>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "-";
  try {
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return date.toLocaleString();
  } catch {
    return String(value);
  }
}

function parseOptionalInt(v: string): number | null {
  const t = v.trim();
  if (!t) return null;
  const n = parseInt(t, 10);
  return Number.isFinite(n) ? n : null;
}
