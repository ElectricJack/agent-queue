import { useRef, useState } from "react";
import { ExclamationTriangleIcon, PencilIcon } from "@heroicons/react/24/outline";
import { useEditTask, useIntelligenceClasses, useProfiles } from "../api/hooks";
import { groupIntelligenceClasses } from "./intelligence-classes/mapping";
import { dedupeProfileOptions } from "../pages/project/Config";
import { PROVIDER_INTENTS, intentHint, intentLabel } from "../pages/metrics/providerAvailabilityFormat";
import { ProviderIntentChip } from "./TaskProviderRouting";

/**
 * The editable field set shared by the full task page and the task pane.
 *
 * One "Edit" toggles every field at once; Save sends only the fields whose
 * value differs from the snapshot taken when editing started, so a task
 * refreshed under the editor never overwrites untouched fields with stale
 * values.  Routing fields (profile, intelligence class, provider intent) are
 * locked while the task is running or claimed because the daemon refuses
 * them then.
 *
 * Provider intent (provider-failover D8/D9) follows the daemon's own rule for
 * a route change: naming a profile makes it *preferred*, so choosing a new
 * profile resets *Pin to this provider* to unchecked, and a pin has to be
 * asked for again.  Clearing the profile leaves nothing to have meant, so
 * the intent becomes *class only*.
 */

export const STATUS_OPTIONS = [
  "PENDING",
  "READY",
  "IN_PROGRESS",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "BLOCKED",
  "CANCELED",
];

export const TASK_TYPE_OPTIONS = [
  "feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan", "sync",
];

export const INTEGRATION_MODE_OPTIONS = ["pull_request", "direct"];

export interface EditableTask {
  id: string;
  title?: string;
  status?: string;
  priority?: number | null;
  task_type?: string | null;
  profile_id?: string | null;
  provider_intent?: string | null;
  intelligence_class?: string | null;
  max_retries?: number | null;
  retry_count?: number | null;
  integration_mode?: string | null;
  effective_integration_mode?: string | null;
  integration_mode_source?: string | null;
  skip_verification?: boolean | null;
  assigned_agent?: string | null;
}

export interface FormState {
  title: string;
  status: string;
  priority: string;
  task_type: string;
  profile_id: string;
  /** ``pinned`` | ``preferred`` | ``class_only`` (D8). */
  provider_intent: string;
  intelligence_class: string;
  max_retries: string;
  integration_mode: string;
  skip_verification: boolean;
}

function taskToForm(t: EditableTask | null | undefined): FormState {
  return {
    title: t?.title ?? "",
    status: t?.status ?? "",
    priority: t?.priority != null ? String(t.priority) : "",
    task_type: t?.task_type ?? "",
    profile_id: t?.profile_id ?? "",
    provider_intent: t?.profile_id ? (t?.provider_intent || "class_only") : "class_only",
    intelligence_class: t?.intelligence_class ?? "",
    max_retries: t?.max_retries != null ? String(t.max_retries) : "",
    integration_mode: t?.integration_mode ?? "",
    skip_verification: !!t?.skip_verification,
  };
}

export function parseOptionalInt(v: string): number | null {
  const t = v.trim();
  if (!t) return null;
  const n = parseInt(t, 10);
  return Number.isFinite(n) ? n : null;
}

export function integrationModeDisplay(task: {
  integration_mode?: string | null;
  effective_integration_mode?: string | null;
  integration_mode_source?: string | null;
}): string {
  const effective = task.effective_integration_mode ?? task.integration_mode ?? "pull_request";
  const source = task.integration_mode_source ?? (task.integration_mode ? "task" : "default");
  return source === "task" ? effective : `${effective} (from ${source} policy)`;
}

/**
 * The intent a profile choice starts with: the daemon stores *preferred* for
 * a profile the caller names (D9), so the pin checkbox starts unchecked for a
 * new route; returning to the original profile restores the original intent.
 */
function intentForProfile(profileId: string, baseline: FormState): string {
  if (!profileId) return "class_only";
  if (profileId === baseline.profile_id) return baseline.provider_intent;
  return "preferred";
}

/** What unticking *Pin to this provider* leaves behind. */
function intentWithoutPin(form: FormState, baseline: FormState): string {
  if (!form.profile_id) return "class_only";
  if (form.profile_id === baseline.profile_id && baseline.provider_intent !== "pinned") {
    return baseline.provider_intent;
  }
  return "preferred";
}

/**
 * The intent fields of an edit_task body.
 *
 * Only what the daemon would not do by itself is sent: a new profile is
 * already *preferred* server-side, and a cleared profile is already *class
 * only*.  A pin is always sent as ``pin: true`` — the route picker's
 * checkbox and the intent select mean the same thing by it.
 */
function intentDiff(form: FormState, baseline: FormState): Record<string, unknown> {
  if (!form.profile_id) return {};
  const profileChanged = form.profile_id !== baseline.profile_id;
  const unchanged = profileChanged
    ? form.provider_intent === "preferred"
    : form.provider_intent === baseline.provider_intent;
  if (unchanged) return {};
  return form.provider_intent === "pinned"
    ? { pin: true }
    : { provider_intent: form.provider_intent };
}

/** Build the edit_task body from a form diff; exported so tests can pin the contract. */
export function diffTaskForm(
  task: EditableTask,
  form: FormState,
  baseline: FormState,
): Record<string, unknown> {
  const body: Record<string, unknown> = { task_id: task.id };
  if (form.title !== baseline.title) body.title = form.title;
  if (form.status && form.status !== baseline.status && task.status !== "PAUSED")
    body.status = form.status;
  const priorityNum = parseOptionalInt(form.priority);
  if (priorityNum !== parseOptionalInt(baseline.priority)) body.priority = priorityNum;
  if (form.task_type !== baseline.task_type) body.task_type = form.task_type || null;
  if (form.profile_id !== baseline.profile_id) body.profile_id = form.profile_id || null;
  Object.assign(body, intentDiff(form, baseline));
  if (form.intelligence_class !== baseline.intelligence_class)
    body.intelligence_class = form.intelligence_class || null;
  const retriesNum = parseOptionalInt(form.max_retries);
  if (retriesNum !== parseOptionalInt(baseline.max_retries)) body.max_retries = retriesNum;
  if (form.integration_mode !== baseline.integration_mode)
    body.integration_mode = form.integration_mode || null;
  if (form.skip_verification !== baseline.skip_verification)
    body.skip_verification = form.skip_verification;
  return body;
}

export default function TaskFieldsEditor({
  task,
  compact = false,
  children,
}: {
  task: EditableTask;
  /** Pane styling: smaller heading and tighter grid. */
  compact?: boolean;
  /** Extra read-only rows rendered inside the grid (branch, timestamps, parent…). */
  children?: React.ReactNode;
}) {
  const { data: profiles } = useProfiles();
  const classes = useIntelligenceClasses();
  const editTask = useEditTask();
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(() => taskToForm(task));
  const baseline = useRef<FormState>(taskToForm(task));
  const [fatal, setFatal] = useState<string | null>(null);

  const profileOptions = dedupeProfileOptions(profiles ?? []);
  const classRows = classes.data?.classes ?? [];
  const classGroups = groupIntelligenceClasses(classRows);
  const currentClass = task.intelligence_class ?? "";
  const classKnown = !currentClass || classRows.some((row) => row.id === currentClass);
  const routingLocked = task.status === "IN_PROGRESS" || !!task.assigned_agent;
  const routingHint = routingLocked
    ? "Locked while the task is running or claimed — stop it first."
    : undefined;

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
    const body = diffTaskForm(task, form, baseline.current);
    if (Object.keys(body).length === 1) {
      setEditing(false);
      return;
    }
    try {
      await editTask.mutateAsync(body as Parameters<typeof editTask.mutateAsync>[0]);
      setEditing(false);
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  const heading = compact
    ? "mb-1.5 text-xs font-semibold uppercase text-gray-500"
    : "mb-3 text-sm font-semibold uppercase text-gray-500";
  const grid = compact
    ? "grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm sm:grid-cols-2"
    : "grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm sm:grid-cols-2";

  return (
    <section aria-label="Task details">
      <div className="flex items-center justify-between gap-3">
        <h2 className={heading}>Details</h2>
        {!editing && (
          <button
            type="button"
            onClick={startEdit}
            className="mb-1.5 inline-flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300"
          >
            <PencilIcon className="h-3.5 w-3.5" /> Edit
          </button>
        )}
      </div>
      <div className={grid}>
        {editing && (
          <div className="sm:col-span-2">
            <span className="text-gray-500">Title</span>
            <input
              type="text"
              aria-label="Title"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              className="mt-0.5 w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </div>
        )}
        <ReadField label="Agent" value={task.assigned_agent ?? "—"} />
        <EditableSelect
          label="Status"
          editing={editing && task.status !== "PAUSED"}
          value={editing ? form.status : task.status ?? "—"}
          displayValue={task.status ?? "—"}
          options={STATUS_OPTIONS}
          onChange={(v) => setForm({ ...form, status: v })}
          hint="Admin override — bypasses the state machine."
        />
        <EditableInput
          label="Priority"
          type="number"
          editing={editing}
          value={form.priority}
          displayValue={task.priority != null ? String(task.priority) : "—"}
          onChange={(v) => setForm({ ...form, priority: v })}
        />
        <EditableSelect
          label="Intelligence class"
          editing={editing}
          disabled={routingLocked}
          value={form.intelligence_class}
          displayValue={currentClass || "—"}
          options={[
            "",
            ...(classKnown ? [] : [currentClass]),
            ...classGroups.flatMap((group) => group.rows.map((row) => row.id)),
          ]}
          optionLabel={(v) => (v === "" ? "— none —" : v)}
          onChange={(v) => setForm({ ...form, intelligence_class: v })}
          hint={routingHint ?? (classes.isError ? "Could not load intelligence classes." : undefined)}
        />
        <EditableSelect
          label="Profile"
          editing={editing}
          disabled={routingLocked}
          value={form.profile_id}
          displayValue={task.profile_id ?? "default"}
          options={["", ...profileOptions.map((p) => p.id)]}
          optionLabel={(v) =>
            v === "" ? "— inherit / none —" : profileOptions.find((p) => p.id === v)?.name ?? v
          }
          onChange={(v) =>
            setForm({ ...form, profile_id: v, provider_intent: intentForProfile(v, baseline.current) })
          }
          hint={routingHint}
          extra={
            <label className="mt-1 flex items-center gap-2 text-xs text-gray-300">
              <input
                type="checkbox"
                checked={form.provider_intent === "pinned"}
                disabled={routingLocked || !form.profile_id}
                onChange={(e) =>
                  setForm({
                    ...form,
                    provider_intent: e.target.checked
                      ? "pinned"
                      : intentWithoutPin(form, baseline.current),
                  })
                }
                className="h-3.5 w-3.5 cursor-pointer rounded border-gray-700 bg-gray-900 accent-indigo-500 disabled:cursor-not-allowed"
              />
              <span title="A pinned task holds while its provider is unavailable instead of failing over.">
                Pin to this provider
              </span>
            </label>
          }
        />
        <div>
          <span className="text-gray-500">Provider intent</span>
          {editing ? (
            <>
              <select
                aria-label="Provider intent"
                value={form.provider_intent}
                disabled={routingLocked}
                onChange={(e) => setForm({ ...form, provider_intent: e.target.value })}
                className="mt-0.5 w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none disabled:opacity-50"
              >
                {PROVIDER_INTENTS.map((intent) => (
                  // Pinned and preferred name a provider, so they need a profile.
                  <option key={intent} value={intent} disabled={intent !== "class_only" && !form.profile_id}>
                    {intentLabel(intent)}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-gray-500">{routingHint ?? intentHint(form.provider_intent)}</p>
            </>
          ) : (
            <p className="mt-0.5">
              <ProviderIntentChip intent={task.provider_intent} />
            </p>
          )}
        </div>
        <EditableSelect
          label="Task type"
          editing={editing}
          value={form.task_type}
          displayValue={task.task_type ?? "—"}
          options={[
            "",
            ...(task.task_type && !TASK_TYPE_OPTIONS.includes(task.task_type) ? [task.task_type] : []),
            ...TASK_TYPE_OPTIONS,
          ]}
          optionLabel={(v) => (v === "" ? "— none —" : v)}
          onChange={(v) => setForm({ ...form, task_type: v })}
        />
        <EditableInput
          label="Max retries"
          type="number"
          editing={editing}
          value={form.max_retries}
          displayValue={String(task.max_retries ?? "—")}
          onChange={(v) => setForm({ ...form, max_retries: v })}
        />
        <ReadField label="Retries used" value={`${task.retry_count ?? 0} / ${task.max_retries ?? 3}`} />
        <EditableSelect
          label="Integration mode"
          editing={editing}
          value={form.integration_mode}
          displayValue={integrationModeDisplay(task)}
          options={["", ...INTEGRATION_MODE_OPTIONS]}
          optionLabel={(v) => (v === "" ? "— inherit policy —" : v)}
          onChange={(v) => setForm({ ...form, integration_mode: v })}
          hint="pull_request: push branch + open PR, review pipeline merges. direct: merge to default on completion. Inherit uses the project/system policy."
        />
        <EditableCheckbox
          label="Skip verification"
          editing={editing}
          checked={form.skip_verification}
          displayValue={task.skip_verification ? "Yes" : "No"}
          onChange={(v) => setForm({ ...form, skip_verification: v })}
        />
        {children}
      </div>
      {editing && (
        <div className="mt-3 space-y-3">
          {fatal && (
            <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
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
    </section>
  );
}

export function ReadField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      <p className={mono ? "truncate font-mono text-xs text-gray-300" : "text-gray-300"}>{value}</p>
    </div>
  );
}

function EditableInput({
  label, type, editing, value, displayValue, onChange,
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
          aria-label={label}
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
  label, editing, disabled = false, value, displayValue, options, optionLabel, onChange, hint, extra,
}: {
  label: string;
  editing: boolean;
  disabled?: boolean;
  value: string;
  displayValue: string;
  options: string[];
  optionLabel?: (v: string) => string;
  onChange: (v: string) => void;
  hint?: string;
  /** Rendered under the select while editing (the route picker's pin checkbox). */
  extra?: React.ReactNode;
}) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      {editing ? (
        <>
          <select
            aria-label={label}
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
            className="mt-0.5 w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none disabled:opacity-50"
          >
            {options.map((opt) => (
              <option key={opt} value={opt}>
                {optionLabel ? optionLabel(opt) : opt || "—"}
              </option>
            ))}
          </select>
          {extra}
          {hint && <p className="mt-1 text-xs text-gray-500">{hint}</p>}
        </>
      ) : (
        <p className="text-gray-300">{displayValue}</p>
      )}
    </div>
  );
}

function EditableCheckbox({
  label, editing, checked, displayValue, onChange,
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
