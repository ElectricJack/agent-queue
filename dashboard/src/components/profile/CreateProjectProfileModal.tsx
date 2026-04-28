import { useEffect, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "../Modal";
import { useCreateProjectProfile } from "../../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  existingAgentTypes: string[];
  onCreated: (agentType: string) => void;
}

export default function CreateProjectProfileModal({
  open,
  onClose,
  projectId,
  existingAgentTypes,
  onCreated,
}: Props) {
  const create = useCreateProjectProfile();
  const [agentType, setAgentType] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [seedFromGlobal, setSeedFromGlobal] = useState(true);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setAgentType("");
      setName("");
      setDescription("");
      setSeedFromGlobal(true);
      setFatal(null);
    }
  }, [open]);

  const trimmedType = agentType.trim();
  const hasColon = trimmedType.includes(":");
  const collides = existingAgentTypes.includes(trimmedType);
  const validationError = !trimmedType
    ? null
    : hasColon
      ? "Agent type cannot contain ':'"
      : collides
        ? "An entry for this agent type already exists — edit it from the list instead"
        : null;
  const canSubmit = !!trimmedType && !validationError && !create.isPending;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setFatal(null);
    try {
      await create.mutateAsync({
        project_id: projectId,
        agent_type: trimmedType,
        seed_from_global: seedFromGlobal,
        name: name.trim() || undefined,
        description: description.trim() || undefined,
      });
      onCreated(trimmedType);
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="New project profile">
      <div className="space-y-4">
        <p className="text-xs text-gray-500">
          Creates a project-scoped profile. If a global profile with the same agent type exists,
          you can seed the new override from it; otherwise the profile starts blank and you can
          configure it after creation.
        </p>

        <div className="space-y-3">
          <Field label="Agent type" required hint="No spaces or ':'. Example: weather-checker">
            <input
              autoFocus
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              placeholder="weather-checker"
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-100 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none"
            />
          </Field>

          <Field label="Name" hint="Optional. Defaults to '<agent_type> (project: <project>)'.">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Weather checker"
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-100 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none"
            />
          </Field>

          <Field label="Description" hint="Optional.">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-100 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none"
            />
          </Field>

          <label className="flex cursor-pointer items-start gap-2 text-sm text-gray-300">
            <input
              type="checkbox"
              checked={seedFromGlobal}
              onChange={(e) => setSeedFromGlobal(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-gray-600 bg-gray-950 text-indigo-500 focus:ring-indigo-500"
            />
            <span>
              Seed from matching global profile if one exists
              <span className="block text-xs text-gray-500">
                Has no effect when the agent type is brand new.
              </span>
            </span>
          </label>
        </div>

        {validationError && (
          <div className="text-xs text-amber-300">{validationError}</div>
        )}

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
            onClick={onSubmit}
            disabled={!canSubmit}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {create.isPending ? "Creating..." : "Create profile"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

interface FieldProps {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}

function Field({ label, hint, required, children }: FieldProps) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-1 text-xs font-medium text-gray-300">
        <span>{label}</span>
        {required && <span className="text-red-400">*</span>}
      </div>
      {children}
      {hint && <p className="text-xs text-gray-500">{hint}</p>}
    </div>
  );
}
