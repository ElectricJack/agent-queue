import { useId, useState } from "react";
import { useCreateTask, useIntelligenceClasses, useProjects, type CreateTaskRequest } from "../api/hooks";
import { groupIntelligenceClasses } from "./intelligence-classes/mapping";
import Modal from "./Modal";

interface CreateTaskModalProps {
  open: boolean;
  onClose: () => void;
  defaultProjectId?: string;
  onCreated?: (taskId: string) => void;
}
const TASK_TYPES = ["feature", "bugfix", "refactor", "test", "docs", "chore", "research"];
const inputClass = "w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";

export default function CreateTaskModal({ open, onClose, defaultProjectId, onCreated }: CreateTaskModalProps) {
  const { data: projects } = useProjects();
  const { data: classData, isLoading: classesLoading, error: classesError } = useIntelligenceClasses();
  const intelligenceClasses = classData?.classes ?? [];
  const createTask = useCreateTask();
  const id = useId();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState(defaultProjectId ?? "");
  const [priority, setPriority] = useState(100);
  const [taskType, setTaskType] = useState("");
  const [integrationMode, setIntegrationMode] = useState("");
  // The class is required: a task that reaches the queue without one waits on
  // the assignment router, and the operator creating it by hand already knows
  // how much reasoning the work needs.
  const [intelligenceClass, setIntelligenceClass] = useState("");
  const valid = !!title.trim() && !!projectId && !!intelligenceClass && Number.isInteger(priority);
  const error: unknown = createTask.error;
  const errorMessage = error instanceof Error ? error.message : error && typeof error === "object" && "error" in error
    ? String(error.error) : error ? "Could not create task. Please try again." : null;

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!valid || createTask.isPending) return;
    const body: CreateTaskRequest = { title: title.trim(), project_id: projectId, intelligence_class: intelligenceClass };
    if (description.trim()) body.description = description.trim();
    if (priority !== 100) body.priority = priority;
    if (taskType) body.task_type = taskType;
    if (integrationMode) body.integration_mode = integrationMode;
    createTask.mutate(body, {
      onSuccess: (data) => {
        const taskId = data.task_id || data.created;
        if (taskId) onCreated?.(taskId);
        setTitle(""); setDescription(""); setProjectId(defaultProjectId ?? "");
        setPriority(100); setTaskType(""); setIntegrationMode(""); setIntelligenceClass("");
        onClose();
      },
    });
  };

  return (
    <Modal open={open} onClose={() => { if (!createTask.isPending) onClose(); }} title="Create Task">
      <form aria-label="Create task" onSubmit={handleSubmit}>
        <fieldset disabled={createTask.isPending} className="space-y-4 disabled:opacity-70">
          <div>
            <label htmlFor={`${id}-title`} className="mb-1 block text-sm text-gray-400">Title *</label>
            <input id={`${id}-title`} type="text" value={title} onChange={(e) => setTitle(e.target.value)}
              className={inputClass} placeholder="Task title" required autoFocus />
          </div>
          <div>
            <label htmlFor={`${id}-description`} className="mb-1 block text-sm text-gray-400">Description</label>
            <textarea id={`${id}-description`} value={description} onChange={(e) => setDescription(e.target.value)}
              rows={3} className={inputClass} placeholder="Describe what needs to be done…" />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor={`${id}-project`} className="mb-1 block text-sm text-gray-400">Project</label>
              <select id={`${id}-project`} value={projectId} onChange={(e) => setProjectId(e.target.value)} className={inputClass} required>
                <option value="">Select project</option>
                {(projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor={`${id}-priority`} className="mb-1 block text-sm text-gray-400">Priority</label>
              <input id={`${id}-priority`} type="number" step="1" value={Number.isNaN(priority) ? "" : priority}
                onChange={(e) => setPriority(e.target.valueAsNumber)} className={inputClass} />
            </div>
          </div>
          <div>
            <label htmlFor={`${id}-class`} className="mb-1 block text-sm text-gray-400">Intelligence class *</label>
            <select id={`${id}-class`} value={intelligenceClass} onChange={(e) => setIntelligenceClass(e.target.value)}
              className={inputClass} required disabled={classesLoading}>
              <option value="">{classesLoading ? "Loading intelligence classes…" : "Select intelligence class"}</option>
              {groupIntelligenceClasses(intelligenceClasses).map(({ label, rows }) => (
                <optgroup key={label} label={label}>
                  {rows.map((row) => <option key={row.id} value={row.id}>{row.id}{row.description ? ` — ${row.description}` : ""}</option>)}
                </optgroup>
              ))}
            </select>
            {classesError && <p className="mt-1 text-xs text-red-400">Could not load intelligence classes.</p>}
          </div>
          <div>
            <label htmlFor={`${id}-type`} className="mb-1 block text-sm text-gray-400">Type</label>
            <select id={`${id}-type`} value={taskType} onChange={(e) => setTaskType(e.target.value)} className={inputClass}>
              <option value="">None</option>
              {TASK_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor={`${id}-integration`} className="mb-1 block text-sm text-gray-400">Integration mode</label>
            <select id={`${id}-integration`} value={integrationMode} onChange={(e) => setIntegrationMode(e.target.value)} className={inputClass}>
              <option value="">Inherit project/system policy</option>
              <option value="pull_request">pull_request — push branch + open PR (review pipeline merges)</option>
              <option value="direct">direct — merge to default branch on completion</option>
            </select>
          </div>
          {errorMessage && <p role="alert" className="rounded border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-300">{errorMessage}</p>}
          <div className="flex justify-end gap-2 border-t border-gray-800 pt-3">
            <button type="button" onClick={onClose} className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700">Cancel</button>
            <button type="submit" disabled={!valid || createTask.isPending}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">
              {createTask.isPending ? "Creating…" : "Create Task"}
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  );
}
