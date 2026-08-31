import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  ExclamationTriangleIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import {
  useEditProject,
  usePauseProject,
  useProject,
  useProjectProfiles,
  useResumeProject,
} from "../../api/hooks";
import DeleteProjectModal from "../../components/DeleteProjectModal";
import ProjectProfiles from "./Profiles";

export interface FormState {
  name: string;
  repo_default_branch: string;
  default_profile_id: string;
  max_concurrent_agents: string;
  credit_weight: string;
  budget_limit: string;
  discord_channel_id: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  repo_default_branch: "",
  default_profile_id: "",
  max_concurrent_agents: "",
  credit_weight: "",
  budget_limit: "",
  discord_channel_id: "",
};

export default function ProjectConfig() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);
  const { data: profiles } = useProjectProfiles(projectId);
  const editProject = useEditProject();
  const pauseProject = usePauseProject();
  const resumeProject = useResumeProject();

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [fatal, setFatal] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    if (project) setForm(projectToForm(project));
  }, [project]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!project) return <p className="text-sm text-gray-500">Project not found.</p>;

  const profileOptions = profileOptionsFromRows(profiles?.agent_types ?? []);

  const startEdit = () => {
    setForm(projectToForm(project));
    setFatal(null);
    setEditing(true);
  };

  const cancel = () => {
    setForm(projectToForm(project));
    setFatal(null);
    setEditing(false);
  };

  const save = async () => {
    setFatal(null);
    try {
      const body = {
        project_id: project.id,
        name: form.name.trim() || null,
        repo_default_branch: form.repo_default_branch.trim() || null,
        default_profile_id: form.default_profile_id.trim() || null,
        max_concurrent_agents: parseOptionalInt(form.max_concurrent_agents),
        credit_weight: parseOptionalFloat(form.credit_weight),
        budget_limit: parseOptionalFloat(form.budget_limit),
        discord_channel_id: form.discord_channel_id.trim() || null,
      };
      await editProject.mutateAsync(body);
      setEditing(false);
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  const togglePause = () => {
    if (project.paused) {
      resumeProject.mutate({ project_id: project.id });
    } else {
      pauseProject.mutate({ project_id: project.id });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold uppercase text-gray-500">Project</h2>
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium ${
              project.paused
                ? "bg-amber-500/10 text-amber-300"
                : "bg-emerald-500/10 text-emerald-300"
            }`}
          >
            {project.paused ? "Paused" : "Active"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={togglePause}
            disabled={pauseProject.isPending || resumeProject.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50"
          >
            {project.paused ? (
              <>
                <PlayIcon className="h-4 w-4" /> Resume
              </>
            ) : (
              <>
                <PauseIcon className="h-4 w-4" /> Pause
              </>
            )}
          </button>
          {!editing && (
            <button
              type="button"
              onClick={startEdit}
              className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
            >
              <PencilIcon className="h-4 w-4" /> Edit
            </button>
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-800">
        <Row label="ID" striped={false}>
          <span className="font-mono text-gray-400">{project.id}</span>
        </Row>
        <Row label="Name" striped>
          {editing ? (
            <TextInput value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
          ) : (
            project.name
          )}
        </Row>
        <Row label="Default branch" striped={false}>
          {editing ? (
            <TextInput
              value={form.repo_default_branch}
              onChange={(v) => setForm({ ...form, repo_default_branch: v })}
              placeholder="main"
            />
          ) : (
            project.repo_default_branch ?? "—"
          )}
        </Row>
        <Row label="Repo URL" striped>
          <span className="font-mono text-xs text-gray-400">{project.repo_url ?? "—"}</span>
        </Row>
        <Row label="Default profile" striped={false}>
          {editing ? (
            <select
              value={form.default_profile_id}
              onChange={(e) => setForm({ ...form, default_profile_id: e.target.value })}
              className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
            >
              <option value="">— inherit / none —</option>
              {profileOptions.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.id})
                </option>
              ))}
            </select>
          ) : (
            project.default_profile_id ?? "—"
          )}
        </Row>
        <Row label="Discord channel" striped>
          {editing ? (
            <TextInput
              value={form.discord_channel_id}
              onChange={(v) => setForm({ ...form, discord_channel_id: v })}
              placeholder="(channel id)"
            />
          ) : (
            project.discord_channel_id ?? "—"
          )}
        </Row>
        <Row label="Max concurrent agents" striped={false}>
          {editing ? (
            <NumberInput
              value={form.max_concurrent_agents}
              onChange={(v) => setForm({ ...form, max_concurrent_agents: v })}
              min={1}
            />
          ) : (
            String(project.max_concurrent_agents ?? "—")
          )}
        </Row>
        <Row label="Credit weight" striped>
          {editing ? (
            <NumberInput
              value={form.credit_weight}
              onChange={(v) => setForm({ ...form, credit_weight: v })}
              step={0.1}
              min={0}
            />
          ) : (
            String(project.credit_weight ?? "—")
          )}
        </Row>
        <Row label="Budget limit" striped={false}>
          {editing ? (
            <NumberInput
              value={form.budget_limit}
              onChange={(v) => setForm({ ...form, budget_limit: v })}
              step={0.01}
              min={0}
              placeholder="(no limit)"
            />
          ) : project.budget_limit != null ? (
            String(project.budget_limit)
          ) : (
            "—"
          )}
        </Row>
      </div>

      {editing && (
        <div className="space-y-3">
          {fatal && (
            <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
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
              disabled={editProject.isPending}
              className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            >
              {editProject.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}

      <section aria-labelledby="profile-overrides-heading" className="space-y-3 border-t border-gray-800 pt-6">
        <h2 id="profile-overrides-heading" className="text-sm font-semibold uppercase text-gray-500">
          Profile overrides
        </h2>
        <ProjectProfiles />
      </section>

      <section className="rounded-lg border border-red-500/30 bg-red-500/5 p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-red-300">Danger zone</h3>
            <p className="text-xs text-red-300/70">
              Deleting a project removes its tasks, workspaces, and constraints. This cannot be
              undone.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDeleteOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm font-medium text-red-300 transition-colors hover:bg-red-500/20 hover:text-red-200"
          >
            <TrashIcon className="h-4 w-4" />
            Delete project
          </button>
        </div>
      </section>

      <DeleteProjectModal
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        projectId={project.id}
        projectName={project.name}
      />
    </div>
  );
}

function Row({
  label,
  striped,
  children,
}: {
  label: string;
  striped: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`grid grid-cols-3 items-center gap-4 px-4 py-3 text-sm ${
        striped ? "bg-gray-900/50" : "bg-gray-900"
      }`}
    >
      <dt className="text-gray-400">{label}</dt>
      <dd className="col-span-2 text-gray-200">{children}</dd>
    </div>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
    />
  );
}

function NumberInput({
  value,
  onChange,
  min,
  step,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  min?: number;
  step?: number;
  placeholder?: string;
}) {
  return (
    <input
      type="number"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      min={min}
      step={step}
      placeholder={placeholder}
      className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
    />
  );
}

export interface ProjectData {
  name?: string | null;
  repo_default_branch?: string | null;
  default_profile_id?: string | null;
  max_concurrent_agents?: number | null;
  credit_weight?: number | null;
  budget_limit?: number | null;
  discord_channel_id?: string | null;
}

export function profileOptionsFromRows(
  rows: { scoped?: { id: string; name: string } | null; global?: { id: string; name: string } | null }[],
): { id: string; name: string }[] {
  return rows
    .flatMap((row) => [row.scoped, row.global].filter(Boolean) as { id: string; name: string }[])
    .filter((p, i, arr) => arr.findIndex((q) => q.id === p.id) === i);
}

export function projectToForm(p: ProjectData): FormState {
  return {
    name: p.name ?? "",
    repo_default_branch: p.repo_default_branch ?? "",
    default_profile_id: p.default_profile_id ?? "",
    max_concurrent_agents:
      p.max_concurrent_agents != null ? String(p.max_concurrent_agents) : "",
    credit_weight: p.credit_weight != null ? String(p.credit_weight) : "",
    budget_limit: p.budget_limit != null ? String(p.budget_limit) : "",
    discord_channel_id: p.discord_channel_id ?? "",
  };
}

export function parseOptionalInt(v: string): number | null {
  const t = v.trim();
  if (!t) return null;
  const n = parseInt(t, 10);
  return Number.isFinite(n) ? n : null;
}

export function parseOptionalFloat(v: string): number | null {
  const t = v.trim();
  if (!t) return null;
  const n = parseFloat(t);
  return Number.isFinite(n) ? n : null;
}
