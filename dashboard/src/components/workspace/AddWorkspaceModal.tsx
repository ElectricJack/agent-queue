import { useEffect, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "../Modal";
import { useAddWorkspace } from "../../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
}

export default function AddWorkspaceModal({ open, onClose, projectId }: Props) {
  const add = useAddWorkspace(projectId);
  const [source, setSource] = useState<"clone" | "link" | "init">("link");
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setSource("link");
      setPath("");
      setName("");
      setFatal(null);
    }
  }, [open]);

  const onConfirm = async () => {
    setFatal(null);
    try {
      await add.mutateAsync({
        source,
        path: path.trim() || undefined,
        name: name.trim() || undefined,
      });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  const pathRequired = source === "link";

  return (
    <Modal open={open} onClose={onClose} title="Add workspace">
      <div className="space-y-4">
        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Source
          </label>
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as "clone" | "link" | "init")}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            <option value="link">link (point at an existing checkout)</option>
            <option value="clone">clone (clone the project's repo_url)</option>
            <option value="init">init (run git init in a fresh dir)</option>
          </select>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Path {pathRequired ? <span className="text-red-400">*</span> : <span className="text-gray-600">(optional — auto-generated)</span>}
          </label>
          <input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/absolute/path/to/checkout"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Name (optional)
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. main, feature-x"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </div>

        {fatal && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={add.isPending || (pathRequired && !path.trim())}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {add.isPending ? "Adding..." : "Add workspace"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
