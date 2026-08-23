import { useEffect } from "react";
import { CheckIcon, ArrowUturnLeftIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useProjectProfiles, useEditProjectProfile } from "../../../api/hooks";
import { profileToForm, type ProfileFormState as FormState } from "../../../components/profile/profileForm";
import { Section, Field } from "../../../components/profile/FormSection";
import IntelligenceClassPicker from "../../../components/profile/IntelligenceClassPicker";
import McpServerSelector from "../../../components/profile/McpServerSelector";
import ToolPicker from "../../../components/profile/ToolPicker";
import { useDirtyForm } from "../useDirtyForm";
import { fullSettingsRoute } from "../fullSettingsRoute";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "project-profile" }>;

export default function ProjectProfileSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const navigate = useNavigate();
  const { data: rows } = useProjectProfiles(args.projectId);
  const row = rows?.agent_types?.find((r) => r.agent_type === args.subjectId);
  const scoped = row?.scoped ?? null;
  const global = row?.global ?? null;
  const seed = scoped ?? global;

  const edit = useEditProjectProfile();
  const { value: form, setValue: setForm, dirty, resetBaseline } = useDirtyForm<FormState>(
    profileToForm(seed),
  );

  useEffect(() => {
    if (seed) resetBaseline(profileToForm(seed));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const onMcpChange = (next: string[]) => {
    setForm((prev) => {
      const removed = prev.mcp_servers.filter((n) => !next.includes(n));
      if (removed.length === 0) return { ...prev, mcp_servers: next };
      const dropPrefixes = removed.map((n) => `mcp__${n}__`);
      const allowed = prev.allowed_tools.filter((t) => !dropPrefixes.some((p) => t.startsWith(p)));
      return { ...prev, mcp_servers: next, allowed_tools: allowed };
    });
  };

  const save = async () => {
    await edit.mutateAsync({
      project_id: args.projectId,
      agent_type: args.subjectId,
      name: form.name || null,
      description: form.description || null,
      default_class: form.default_class || "",
      permission_mode: form.permission_mode || null,
      system_prompt_suffix: form.system_prompt_suffix || null,
      allowed_tools: form.allowed_tools,
      mcp_servers: form.mcp_servers,
    } as unknown as Parameters<typeof edit.mutateAsync>[0]);
    resetBaseline(form);
  };

  useEffect(() => {
    setToolbar([
      {
        id: "save",
        label: "Save",
        icon: CheckIcon,
        onClick: save,
        // No "create override" flow exists in ProfileEditDrawer.tsx either
        // (spec §5.3/§13) — Save stays disabled with no scoped row.
        disabled: !scoped || !dirty || edit.isPending,
      },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => seed && resetBaseline(profileToForm(seed)),
        disabled: !dirty,
      },
      {
        id: "open-full",
        label: "Open full settings page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: () => navigate(fullSettingsRoute(args)),
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, edit.isPending, form, scoped, seed]);

  if (!row) return <p className="text-sm text-gray-500">Loading profile…</p>;

  return (
    <div className="space-y-6 text-sm">
      {!scoped && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          No project override exists yet.
        </p>
      )}

      <Section title="Basics">
        <Field label="Name">
          <input
            aria-label="Name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Description">
          <input
            aria-label="Description"
            value={form.description}
            onChange={(e) => set("description", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section
        title="Intelligence class & permissions"
        hint="Picks the model + reasoning tier per provider. See Settings → Intelligence Classes for the matrix."
      >
        <Field label="Intelligence class">
          <IntelligenceClassPicker value={form.default_class} onChange={(v) => set("default_class", v)} />
        </Field>
        <Field label="Permission mode">
          <input
            aria-label="Permission mode"
            value={form.permission_mode}
            onChange={(e) => set("permission_mode", e.target.value)}
            placeholder="acceptEdits"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="System prompt suffix">
        <textarea
          aria-label="System prompt suffix"
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
        <McpServerSelector projectId={args.projectId} value={form.mcp_servers} onChange={onMcpChange} />
      </Section>

      <Section title="Allowed tools" hint="Tools the agent may invoke. Groups appear for the servers selected above.">
        <ToolPicker
          projectId={args.projectId}
          value={form.allowed_tools}
          onChange={(t) => set("allowed_tools", t)}
          enabledServers={form.mcp_servers}
          model=""
        />
      </Section>

      {edit.isError && (
        <p className="text-sm text-red-400">{(edit.error as Error)?.message ?? "Save failed."}</p>
      )}
    </div>
  );
}
