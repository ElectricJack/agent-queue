import { useEffect, useState } from "react";
import { ExclamationTriangleIcon, XMarkIcon } from "@heroicons/react/24/outline";
import { useEditWorkspace, type Workspace as WorkspaceSummary } from "../../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  workspace: WorkspaceSummary;
}

export default function EditWorkspaceDrawer({
  open,
  onClose,
  projectId,
  workspace,
}: Props) {
  const edit = useEditWorkspace(projectId);
  const [name, setName] = useState(workspace.name ?? "");
  const [path, setPath] = useState(workspace.workspace_path);
  const [sourceType, setSourceType] = useState(workspace.source_type ?? "");
  const [enabled, setEnabled] = useState(workspace.enabled !== false);
  const [force, setForce] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName(workspace.name ?? "");
      setPath(workspace.workspace_path);
      setSourceType(workspace.source_type ?? "");
      setEnabled(workspace.enabled !== false);
      setForce(false);
      setFatal(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspace.id]);

  if (!open) return null;

  const isLocked = !!workspace.locked_by_agent_id;
  const pathChanged = path !== workspace.workspace_path;

  const onSave = async () => {
    setFatal(null);
    try {
      const body: Record<string, unknown> = { workspace_id: workspace.id };
      if (name !== (workspace.name ?? "")) body.name = name || null;
      if (pathChanged) body.workspace_path = path;
      if (sourceType !== (workspace.source_type ?? "")) body.source_type = sourceType;
      if (enabled !== (workspace.enabled !== false)) body.enabled = enabled;
      if (force) body.force = true;
      await edit.mutateAsync(body as Parameters<typeof edit.mutateAsync>[0]);
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/60" onClick={onClose} aria-hidden />
      <aside className="flex h-full w-full max-w-xl flex-col border-l border-gray-700 bg-gray-900 shadow-2xl">
        <header className="flex items-start justify-between gap-4 border-b border-gray-700 px-6 py-4">
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">Edit workspace</p>
            <h2 className="text-lg font-semibold text-gray-100">{workspace.name || workspace.id}</h2>
            <p className="mt-1 font-mono text-xs text-gray-500">{workspace.id}</p>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200"
          >
            <XMarkIcon className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-6 py-5 text-sm">
          <Field label="Name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </Field>

          <Field
            label="Path"
            hint={
              isLocked
                ? "Workspace is locked by a task; path edits are blocked. Disable first or wait for the task to finish."
                : undefined
            }
          >
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              disabled={isLocked}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            />
          </Field>

          <Field label="Source type">
            <select
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
            >
              <option value="link">link</option>
              <option value="clone">clone</option>
              <option value="init">init</option>
            </select>
          </Field>

          <div className="rounded-md border border-gray-800 bg-gray-950 p-3">
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="mt-0.5 h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-indigo-500"
              />
              <div className="space-y-1">
                <span className="text-sm text-gray-200">Enabled</span>
                <p className="text-xs text-gray-500">
                  When unchecked, the orchestrator skips this workspace when assigning new
                  tasks. Existing locked tasks finish normally.
                </p>
              </div>
            </label>
          </div>

          {pathChanged && !isLocked && (
            <div className="rounded-md border border-gray-800 bg-gray-950 p-3">
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={force}
                  onChange={(e) => setForce(e.target.checked)}
                  className="mt-0.5 h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-amber-500"
                />
                <div className="space-y-1">
                  <span className="text-sm text-amber-300">Force (skip directory check)</span>
                  <p className="text-xs text-gray-500">
                    By default the daemon refuses if the new path doesn't exist on disk. Use
                    this when relocating before the directory is set up.
                  </p>
                </div>
              </label>
            </div>
          )}
        </div>

        {fatal && (
          <div className="flex items-start gap-2 border-t border-red-500/30 bg-red-500/10 px-6 py-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        <footer className="flex items-center justify-end gap-2 border-t border-gray-700 px-6 py-3">
          <button
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            onClick={onSave}
            disabled={edit.isPending}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {edit.isPending ? "Saving..." : "Save"}
          </button>
        </footer>
      </aside>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium uppercase text-gray-500">{label}</label>
      {children}
      {hint && <p className="text-xs text-amber-400">{hint}</p>}
    </div>
  );
}
