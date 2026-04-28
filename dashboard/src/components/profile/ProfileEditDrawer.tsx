import { useEffect, useState } from "react";
import { XMarkIcon, ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import {
  useEditProjectProfile,
  useProjectProfiles,
  type ProfileDetail,
} from "../../api/hooks";
import McpServerSelector from "./McpServerSelector";
import ToolPicker from "./ToolPicker";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  agentType: string;
}

interface FormState {
  name: string;
  description: string;
  model: string;
  permission_mode: string;
  system_prompt_suffix: string;
  allowed_tools: string[];
  mcp_servers: string[];
}

function profileToForm(p: ProfileDetail | null | undefined): FormState {
  return {
    name: p?.name ?? "",
    description: p?.description ?? "",
    model: p?.model ?? "",
    permission_mode: p?.permission_mode ?? "",
    system_prompt_suffix: p?.system_prompt_suffix ?? "",
    allowed_tools: [...(p?.allowed_tools ?? [])],
    mcp_servers: [...(p?.mcp_servers ?? [])],
  };
}

export default function ProfileEditDrawer({ open, onClose, projectId, agentType }: Props) {
  const { data: rows } = useProjectProfiles(projectId);
  const row = rows?.agent_types?.find((r) => r.agent_type === agentType);
  const scoped = row?.scoped ?? null;
  const global = row?.global ?? null;
  const seed = scoped ?? global;

  const edit = useEditProjectProfile();
  const [form, setForm] = useState<FormState>(() => profileToForm(seed));
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(profileToForm(seed));
      setFatal(null);
    }
    // intentionally only reset when the drawer opens or the agent type changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, agentType, projectId]);

  if (!open) return null;

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const onMcpChange = (next: string[]) => {
    setForm((prev) => {
      const removed = prev.mcp_servers.filter((n) => !next.includes(n));
      if (removed.length === 0) {
        return { ...prev, mcp_servers: next };
      }
      // Prune allowed_tools entries that reference servers we just unchecked.
      const dropPrefixes = removed.map((n) => `mcp__${n}__`);
      const allowed = prev.allowed_tools.filter(
        (t) => !dropPrefixes.some((p) => t.startsWith(p)),
      );
      return { ...prev, mcp_servers: next, allowed_tools: allowed };
    });
  };

  const onSave = async () => {
    setFatal(null);
    try {
      await edit.mutateAsync({
        project_id: projectId,
        agent_type: agentType,
        name: form.name || null,
        description: form.description || null,
        model: form.model || null,
        permission_mode: form.permission_mode || null,
        system_prompt_suffix: form.system_prompt_suffix || null,
        allowed_tools: form.allowed_tools,
        mcp_servers: form.mcp_servers,
      });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div
        className="flex-1 bg-black/60"
        onClick={onClose}
        aria-hidden
      />
      <aside className="flex h-full w-full max-w-2xl flex-col border-l border-gray-700 bg-gray-900 shadow-2xl">
        <header className="flex items-start justify-between gap-4 border-b border-gray-700 px-6 py-4">
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">
              {scoped ? "Edit project profile" : "Project profile"}
            </p>
            <h2 className="text-lg font-semibold text-gray-100">{agentType}</h2>
            {!scoped && (
              <p className="mt-1 text-xs text-amber-400">
                No project override exists yet. Use "Add project override" to create one.
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200"
          >
            <XMarkIcon className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5 text-sm">
          <Section title="Basics">
            <Field label="Name">
              <input
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
              />
            </Field>
            <Field label="Description">
              <input
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
                className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
              />
            </Field>
          </Section>

          <Section title="Model & permissions">
            <Field label="Model">
              <input
                value={form.model}
                onChange={(e) => set("model", e.target.value)}
                placeholder="claude-sonnet-4-6"
                className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
              />
            </Field>
            <Field label="Permission mode">
              <input
                value={form.permission_mode}
                onChange={(e) => set("permission_mode", e.target.value)}
                placeholder="acceptEdits"
                className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
              />
            </Field>
          </Section>

          <Section title="System prompt suffix">
            <textarea
              value={form.system_prompt_suffix}
              onChange={(e) => set("system_prompt_suffix", e.target.value)}
              rows={5}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </Section>

          <Section
            title="MCP servers"
            hint="Servers this profile may connect to. The embedded agent-queue server is always included."
          >
            <McpServerSelector
              projectId={projectId}
              value={form.mcp_servers}
              onChange={onMcpChange}
            />
          </Section>

          <Section
            title="Allowed tools"
            hint="Tools the agent may invoke. Groups appear for the servers selected above."
          >
            <ToolPicker
              projectId={projectId}
              value={form.allowed_tools}
              onChange={(t) => set("allowed_tools", t)}
              enabledServers={form.mcp_servers}
              model={form.model}
            />
          </Section>
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
            disabled={!scoped || edit.isPending}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {edit.isPending ? "Saving..." : "Save"}
          </button>
        </footer>
      </aside>
    </div>
  );
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500">{title}</h3>
        {hint && <p className="mt-0.5 text-xs text-gray-600">{hint}</p>}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium uppercase text-gray-500">{label}</label>
      {children}
    </div>
  );
}
