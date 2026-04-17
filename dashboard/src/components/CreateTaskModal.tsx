import { useState } from "react";
import { useCreateTask, useProjects } from "../api/hooks";
import Modal from "./Modal";

interface CreateTaskModalProps {
  open: boolean;
  onClose: () => void;
  defaultProjectId?: string;
}

const TASK_TYPES = ["feature", "bugfix", "refactor", "test", "docs", "chore", "research"];

export default function CreateTaskModal({ open, onClose, defaultProjectId }: CreateTaskModalProps) {
  const { data: projects } = useProjects();
  const createTask = useCreateTask();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState(defaultProjectId ?? "");
  const [priority, setPriority] = useState(100);
  const [taskType, setTaskType] = useState("");
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [autoApprovePlan, setAutoApprovePlan] = useState(false);

  const reset = () => {
    setTitle("");
    setDescription("");
    setProjectId(defaultProjectId ?? "");
    setPriority(100);
    setTaskType("");
    setRequiresApproval(false);
    setAutoApprovePlan(false);
  };

  const handleSubmit = () => {
    const body: Record<string, unknown> = { title };
    if (description) body.description = description;
    if (projectId) body.project_id = projectId;
    if (priority !== 100) body.priority = priority;
    if (taskType) body.task_type = taskType;
    if (requiresApproval) body.requires_approval = true;
    if (autoApprovePlan) body.auto_approve_plan = true;

    createTask.mutate(body, {
      onSuccess: () => {
        reset();
        onClose();
      },
    });
  };

  return (
    <Modal open={open} onClose={onClose} title="Create Task">
      <div className="space-y-4">
        <div>
          <label className="mb-1 block text-sm text-gray-400">Title *</label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            placeholder="Task title"
            autoFocus
          />
        </div>

        <div>
          <label className="mb-1 block text-sm text-gray-400">Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
            placeholder="Describe what needs to be done..."
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="mb-1 block text-sm text-gray-400">Project</label>
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            >
              <option value="">Select project</option>
              {(projects ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name || p.id}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="mb-1 block text-sm text-gray-400">Priority</label>
            <input
              type="number"
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
              className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </div>
        </div>

        <div>
          <label className="mb-1 block text-sm text-gray-400">Type</label>
          <select
            value={taskType}
            onChange={(e) => setTaskType(e.target.value)}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">None</option>
            {TASK_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>

        <div className="flex gap-6">
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input
              type="checkbox"
              checked={requiresApproval}
              onChange={(e) => setRequiresApproval(e.target.checked)}
              className="rounded border-gray-600 bg-gray-800"
            />
            Requires approval
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input
              type="checkbox"
              checked={autoApprovePlan}
              onChange={(e) => setAutoApprovePlan(e.target.checked)}
              className="rounded border-gray-600 bg-gray-800"
            />
            Auto-approve plan
          </label>
        </div>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!title.trim() || !projectId || createTask.isPending}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {createTask.isPending ? "Creating..." : "Create Task"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
