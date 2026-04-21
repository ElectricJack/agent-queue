import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ExclamationTriangleIcon, CheckCircleIcon } from "@heroicons/react/24/outline";
import Modal from "./Modal";
import TriggerPicker from "./TriggerPicker";
import { useCreatePlaybook, useProjects } from "../api/hooks";

type ScopeKind = "system" | "project" | "agent-type";

interface Props {
  open: boolean;
  onClose: () => void;
}

function frontmatterScope(scopeKind: "system" | "project" | "agent-type", agentType: string): string {
  // The project id is derived from the vault path — frontmatter scope is just "project".
  // Agent-type scopes encode the type in the frontmatter because it isn't uniquely
  // recoverable from the path.
  if (scopeKind === "agent-type") return `agent-type:${agentType.trim()}`;
  return scopeKind;
}

function makeTemplate(id: string, frontScope: string, triggers: string[]): string {
  const list = triggers.length > 0 ? triggers : ["manual"];
  const triggerLines = list.map((t) => `  - ${t}`).join("\n");
  return `---
id: ${id}
triggers:
${triggerLines}
scope: ${frontScope}
---

# ${id}

Describe what this playbook does, when it fires, and what a successful run
looks like. Compiled to a graph by the LLM on save.

## Steps

- Step one — what to inspect or gather.
- Step two — what to decide.
- Step three — what side effect to produce.
`;
}

export default function CreatePlaybookModal({ open, onClose }: Props) {
  const create = useCreatePlaybook();
  const navigate = useNavigate();
  const { data: projects } = useProjects();

  const [id, setId] = useState("");
  const [scopeKind, setScopeKind] = useState<ScopeKind>("system");
  const [projectId, setProjectId] = useState("");
  const [agentType, setAgentType] = useState("");
  const [triggers, setTriggers] = useState<string[]>(["manual"]);
  const [useCustomMarkdown, setUseCustomMarkdown] = useState(false);
  const [customMarkdown, setCustomMarkdown] = useState("");
  const [fatal, setFatal] = useState<string | null>(null);

  const projectList = projects ?? [];

  // Default the project picker to the first project whenever projects load
  useEffect(() => {
    if (scopeKind === "project" && !projectId && projectList[0]) {
      setProjectId(projectList[0].id);
    }
  }, [scopeKind, projectId, projectList]);

  // Reset state when the modal is closed
  useEffect(() => {
    if (!open) {
      setId("");
      setScopeKind("system");
      setProjectId("");
      setAgentType("");
      setTriggers(["manual"]);
      setUseCustomMarkdown(false);
      setCustomMarkdown("");
      setFatal(null);
    }
  }, [open]);

  const scopeString = useMemo(() => {
    if (scopeKind === "system") return "system";
    if (scopeKind === "project") return projectId ? `project:${projectId}` : "project:";
    return agentType ? `agent-type:${agentType}` : "agent-type:";
  }, [scopeKind, projectId, agentType]);

  const canSubmit =
    id.trim() &&
    /^[a-z0-9][a-z0-9-]*$/.test(id.trim()) &&
    (scopeKind === "system" ||
      (scopeKind === "project" && projectId) ||
      (scopeKind === "agent-type" && agentType.trim())) &&
    !create.isPending;

  const onSubmit = async () => {
    setFatal(null);
    const cleanId = id.trim();
    const frontScope = frontmatterScope(scopeKind, agentType);
    const markdown = useCustomMarkdown
      ? customMarkdown
      : makeTemplate(cleanId, frontScope, triggers);

    try {
      const result = await create.mutateAsync({
        playbook_id: cleanId,
        scope: scopeString,
        markdown,
      });
      if (result.error) {
        setFatal(result.error);
        return;
      }
      // Success — navigate to the new detail page so the author can iterate
      // and trigger the compile from the Source tab.
      navigate(`/playbooks/${encodeURIComponent(cleanId)}`);
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Create Playbook">
      <div className="space-y-4">
        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            ID
          </label>
          <input
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="my-playbook"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
          <p className="mt-1 text-xs text-gray-500">
            lowercase letters, digits, and dashes. Becomes the filename.
          </p>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Scope
          </label>
          <div className="flex gap-2">
            {(["system", "project", "agent-type"] as ScopeKind[]).map((k) => (
              <button
                key={k}
                onClick={() => setScopeKind(k)}
                className={`rounded-md px-3 py-1 text-sm ${
                  scopeKind === k
                    ? "bg-indigo-500/20 text-indigo-300"
                    : "bg-gray-800 text-gray-400 hover:text-gray-200"
                }`}
              >
                {k}
              </button>
            ))}
          </div>
        </div>

        {scopeKind === "project" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
              Project
            </label>
            {projectList.length === 0 ? (
              <p className="text-sm text-gray-500">No projects configured.</p>
            ) : (
              <select
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
              >
                {projectList.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name || p.id}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {scopeKind === "agent-type" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
              Agent type
            </label>
            <input
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              placeholder="coding"
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </div>
        )}

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Triggers
          </label>
          <TriggerPicker value={triggers} onChange={setTriggers} />
        </div>

        <div>
          <label className="flex items-center gap-2 text-sm text-gray-400">
            <input
              type="checkbox"
              checked={useCustomMarkdown}
              onChange={(e) => setUseCustomMarkdown(e.target.checked)}
            />
            Start from my own markdown instead of the template
          </label>
          {useCustomMarkdown && (
            <textarea
              value={customMarkdown}
              onChange={(e) => setCustomMarkdown(e.target.value)}
              spellCheck={false}
              className="mt-2 h-48 w-full resize-none rounded-md border border-gray-700 bg-gray-950 p-3 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
              placeholder="---&#10;id: ...&#10;triggers:&#10;  - ...&#10;scope: ...&#10;---&#10;&#10;Body..."
            />
          )}
        </div>

        {fatal && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        {create.isSuccess && !fatal && (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">
            <CheckCircleIcon className="h-4 w-4" />
            Created — opening the Source tab so you can iterate and compile.
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
            {create.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
