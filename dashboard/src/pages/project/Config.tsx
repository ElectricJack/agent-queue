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
  useResumeProject,
} from "../../api/hooks";
import DeleteProjectModal from "../../components/DeleteProjectModal";
import PolicyProfiles from "./PolicyProfiles";
import GitIdentitySection from "./GitIdentitySection";
import {
  GIT_IDENTITY_SECTION_HINT,
  type GitIdentityFieldErrors,
  type GitIdentityFormFields,
  type GitIdentityProjectData,
  gitIdentityErrors,
  gitIdentityFormFields,
  gitIdentityPayload,
  gitIdentitySaveError,
  hasGitIdentityErrors,
} from "./gitIdentity";

export interface FormState extends GitIdentityFormFields {
  name: string;
  repo_default_branch: string;
  max_concurrent_agents: string;
  credit_weight: string;
  budget_limit: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  repo_default_branch: "",
  max_concurrent_agents: "",
  credit_weight: "",
  budget_limit: "",
  git_identity_override: false,
  git_identity_name: "",
  git_identity_email: "",
};

export default function ProjectConfig() {
  const { projectId = "" } = useParams();
  const { data: project, isLoading } = useProject(projectId);
  const editProject = useEditProject();
  const pauseProject = usePauseProject();
  const resumeProject = useResumeProject();

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [fatal, setFatal] = useState<string | null>(null);
  const [identityServerErrors, setIdentityServerErrors] = useState<GitIdentityFieldErrors>({});
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    if (project) setForm(projectToForm(project));
  }, [project]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!project) return <p className="text-sm text-gray-500">Project not found.</p>;

  const startEdit = () => {
    setForm(projectToForm(project));
    setFatal(null);
    setIdentityServerErrors({});
    setEditing(true);
  };

  const cancel = () => {
    setForm(projectToForm(project));
    setFatal(null);
    setIdentityServerErrors({});
    setEditing(false);
  };

  const identityClientErrors = gitIdentityErrors(form);
  const identityInvalid = hasGitIdentityErrors(identityClientErrors);

  // The identity controls also work from the read-only view: the first
  // change opens the editor with it staged, so Save / Cancel still decide.
  const changeIdentity = (next: GitIdentityFormFields) => {
    setIdentityServerErrors({});
    if (!editing) {
      setFatal(null);
      setForm({ ...projectToForm(project), ...next });
      setEditing(true);
    } else {
      setForm((prev) => ({ ...prev, ...next }));
    }
  };

  const save = async () => {
    setFatal(null);
    setIdentityServerErrors({});
    if (identityInvalid) return;
    const identity = gitIdentityPayload(projectToForm(project), form);
    try {
      const body = {
        project_id: project.id,
        name: form.name.trim() || null,
        repo_default_branch: form.repo_default_branch.trim() || null,
        max_concurrent_agents: parseOptionalInt(form.max_concurrent_agents),
        credit_weight: parseOptionalFloat(form.credit_weight),
        budget_limit: parseOptionalFloat(form.budget_limit),
        ...identity,
      };
      await editProject.mutateAsync(body);
      setEditing(false);
    } catch (err) {
      const identityError = "git_identity_name" in identity ? gitIdentitySaveError(err) : null;
      if (identityError) setIdentityServerErrors(identityError);
      else setFatal(err instanceof Error ? err.message : String(err));
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
      <PolicyProfiles projectId={projectId} />
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
        {/* A project has no default profile: its router routes every task.
            Re-binding it is local-operator only (aq project set <p> router). */}
        <Row label="Router" striped={false}>
          <span className="font-mono text-xs text-gray-400">
            {project.assignment_playbook_id ?? "—"}
          </span>
        </Row>
        <Row label="Max concurrent agents" striped>
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

      <section
        aria-labelledby="project-git-identity-heading"
        className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"
      >
        <div>
          <h3 id="project-git-identity-heading" className="text-sm font-semibold text-gray-200">
            Git commit identity
          </h3>
          <p className="mt-0.5 text-xs text-gray-500">{GIT_IDENTITY_SECTION_HINT}</p>
        </div>
        <GitIdentitySection
          identity={project.git_identity}
          value={form}
          onChange={changeIdentity}
          editing={editing}
          errors={{ ...identityServerErrors, ...identityClientErrors }}
        />
      </section>

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
              disabled={editProject.isPending || identityInvalid}
              className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            >
              {editProject.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}

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

export interface ProjectData extends GitIdentityProjectData {
  name?: string | null;
  repo_default_branch?: string | null;
  max_concurrent_agents?: number | null;
  credit_weight?: number | null;
  budget_limit?: number | null;
}

export function projectToForm(p: ProjectData): FormState {
  return {
    name: p.name ?? "",
    repo_default_branch: p.repo_default_branch ?? "",
    max_concurrent_agents:
      p.max_concurrent_agents != null ? String(p.max_concurrent_agents) : "",
    credit_weight: p.credit_weight != null ? String(p.credit_weight) : "",
    budget_limit: p.budget_limit != null ? String(p.budget_limit) : "",
    ...gitIdentityFormFields(p),
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
