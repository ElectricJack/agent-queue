import { useEffect } from "react";
import { CheckIcon, ArrowUturnLeftIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useProject, useProjectProfiles, useEditProject } from "../../../api/hooks";
import {
  type FormState,
  parseOptionalInt,
  parseOptionalFloat,
  projectToForm,
  profileOptionsFromRows,
} from "../../../pages/project/Config";
import { Section, Field } from "../../../components/profile/FormSection";
import { useDirtyForm } from "../useDirtyForm";
import { fullSettingsRoute } from "../fullSettingsRoute";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "project" }>;

export default function ProjectSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const navigate = useNavigate();
  const { data: project, isLoading, error } = useProject(args.subjectId);
  const { data: profiles } = useProjectProfiles(args.subjectId);
  const editProject = useEditProject();
  const { value: form, setValue: setForm, dirty, resetBaseline } = useDirtyForm<FormState>(
    projectToForm(project ?? {}),
  );

  useEffect(() => {
    if (project) resetBaseline(projectToForm(project));
    // resetBaseline is stable across renders (from useState setters); only
    // re-run when the server value actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);

  const save = async () => {
    if (!project) return;
    await editProject.mutateAsync({
      project_id: project.id,
      name: form.name.trim() || null,
      repo_default_branch: form.repo_default_branch.trim() || null,
      default_profile_id: form.default_profile_id.trim() || null,
      max_concurrent_agents: parseOptionalInt(form.max_concurrent_agents),
      credit_weight: parseOptionalFloat(form.credit_weight),
      budget_limit: parseOptionalFloat(form.budget_limit),
      discord_channel_id: form.discord_channel_id.trim() || null,
    });
    resetBaseline(form);
  };

  useEffect(() => {
    setToolbar([
      { id: "save", label: "Save", icon: CheckIcon, onClick: save, disabled: !dirty || editProject.isPending },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => project && resetBaseline(projectToForm(project)),
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
  }, [dirty, editProject.isPending, form, project]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading project…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!project) return <p className="text-sm text-gray-500">Project not found.</p>;

  const profileOptions = profileOptionsFromRows(profiles?.agent_types ?? []);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="space-y-6 text-sm">
      <Section title="Basics">
        <Field label="Name">
          <input
            aria-label="Name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Repo URL">
          <span className="font-mono text-xs text-gray-400">{project.repo_url ?? "—"}</span>
        </Field>
        <Field label="Default branch">
          <input
            aria-label="Default branch"
            value={form.repo_default_branch}
            onChange={(e) => set("repo_default_branch", e.target.value)}
            placeholder="main"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Scheduling">
        <Field label="Default profile">
          <select
            aria-label="Default profile"
            value={form.default_profile_id}
            onChange={(e) => set("default_profile_id", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">— inherit / none —</option>
            {profileOptions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.id})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Max concurrent agents">
          <input
            aria-label="Max concurrent agents"
            type="number"
            min={1}
            value={form.max_concurrent_agents}
            onChange={(e) => set("max_concurrent_agents", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Budget">
        <Field label="Credit weight">
          <input
            aria-label="Credit weight"
            type="number"
            step={0.1}
            min={0}
            value={form.credit_weight}
            onChange={(e) => set("credit_weight", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
        <Field label="Budget limit">
          <input
            aria-label="Budget limit"
            type="number"
            step={0.01}
            min={0}
            placeholder="(no limit)"
            value={form.budget_limit}
            onChange={(e) => set("budget_limit", e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      <Section title="Discord">
        <Field label="Channel id">
          <input
            aria-label="Channel id"
            value={form.discord_channel_id}
            onChange={(e) => set("discord_channel_id", e.target.value)}
            placeholder="(channel id)"
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-gray-200 focus:border-indigo-500 focus:outline-none"
          />
        </Field>
      </Section>

      {editProject.isError && (
        <p className="text-sm text-red-400">
          {(editProject.error as Error)?.message ?? "Save failed."}
        </p>
      )}
    </div>
  );
}
