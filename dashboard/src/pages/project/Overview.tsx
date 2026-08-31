import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import {
  BriefcaseIcon,
  CircleStackIcon,
  ClipboardDocumentIcon,
  ClipboardDocumentListIcon,
  CodeBracketIcon,
  CpuChipIcon,
  ExclamationTriangleIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  Squares2X2Icon,
  UsersIcon,
} from "@heroicons/react/24/outline";
import {
  useAgents,
  useProject,
  useTasks,
  useWorkspaces,
  type Task,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import { workspaceHref } from "../../shell/projectNavigation";

const TERMINAL = new Set(["COMPLETED", "FAILED", "SKIPPED", "CANCELED"]);

export default function ProjectOverview() {
  const { projectId = "" } = useParams();
  const location = useLocation();
  const { data: project } = useProject(projectId);
  const { data: tasks } = useTasks(projectId);
  const { data: agents } = useAgents(projectId);
  const { data: workspaces } = useWorkspaces(projectId);

  const taskList: Task[] = tasks ?? [];
  const agentList = agents ?? [];
  const workspaceList = workspaces ?? [];

  const statusCounts = countBy(taskList, (t) => (t.status ?? "PENDING").toUpperCase());
  const activeTasks = taskList.filter(
    (t) => !TERMINAL.has((t.status ?? "").toUpperCase()),
  );
  const completed = statusCounts.COMPLETED ?? 0;
  const failed = statusCounts.FAILED ?? 0;
  const inProgress = statusCounts.IN_PROGRESS ?? 0;
  const ready = statusCounts.READY ?? 0;
  const total = taskList.length;
  const completedPct = total ? Math.round((completed / total) * 100) : 0;

  const busyAgents = agentList.filter((a) => a.state === "busy").length;
  const lockedWorkspaces = workspaceList.filter((w) => w.locked_by_task_id).length;

  const repoUrl = (project as { repo_url?: string } | undefined)?.repo_url;
  const repoBranch = (project as { repo_default_branch?: string } | undefined)
    ?.repo_default_branch;
  const workspace = (project as { workspace?: string | null } | undefined)?.workspace;
  const discordChannelId = (project as { discord_channel_id?: string | null } | undefined)
    ?.discord_channel_id;
  const creditWeight = (project as { credit_weight?: number } | undefined)?.credit_weight;
  const maxAgents = (project as { max_concurrent_agents?: number } | undefined)
    ?.max_concurrent_agents;
  const budget = (project as { budget_limit?: number | null } | undefined)?.budget_limit;
  const tokensUsed = (project as { total_tokens_used?: number } | undefined)?.total_tokens_used;
  const tokensRecent = (project as { tokens_used_recent?: number } | undefined)?.tokens_used_recent;
  const defaultProfile = (project as { default_profile_id?: string | null } | undefined)
    ?.default_profile_id;

  return (
    <div className="space-y-6">
      {/* Top stats row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          icon={<ClipboardDocumentListIcon className="h-5 w-5 text-blue-400" />}
          label="Tasks"
          value={String(total)}
          hint={`${completed} done · ${completedPct}%`}
        />
        <StatCard
          icon={<CpuChipIcon className="h-5 w-5 text-indigo-400" />}
          label="In progress"
          value={String(inProgress)}
          hint={ready ? `${ready} ready` : "queue clear"}
        />
        <StatCard
          icon={<UsersIcon className="h-5 w-5 text-emerald-400" />}
          label="Agents"
          value={`${busyAgents}/${agentList.length || "—"}`}
          hint={agentList.length ? `${busyAgents} busy` : "none attached"}
        />
        <StatCard
          icon={<BriefcaseIcon className="h-5 w-5 text-amber-400" />}
          label="Workspaces"
          value={`${lockedWorkspaces}/${workspaceList.length || "—"}`}
          hint={workspaceList.length ? `${lockedWorkspaces} locked` : "none attached"}
        />
      </div>

      {/* Repo + paths + launch strip */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Repo */}
        <Card title="Repository" icon={<CodeBracketIcon className="h-4 w-4" />}>
          {repoUrl ? (
            <>
              <CopyRow label="URL" value={repoUrl} mono />
              <CopyRow label="Default branch" value={repoBranch || "main"} mono />
              {isGitHubUrl(repoUrl) && (
                <ExternalLink href={repoUrl} label="Open on GitHub" />
              )}
            </>
          ) : (
            <EmptyRow text="No repo URL configured." />
          )}
        </Card>

        {/* Paths */}
        <Card title="Paths" icon={<CircleStackIcon className="h-4 w-4" />}>
          {workspace ? (
            <CopyRow label="Workspace" value={workspace} mono />
          ) : (
            <EmptyRow text="No project workspace path." />
          )}
          {workspaceList[0]?.workspace_path && workspaceList[0].workspace_path !== workspace && (
            <CopyRow
              label="First worktree"
              value={workspaceList[0].workspace_path}
              mono
            />
          )}
          {workspaceList.length > 1 && (
            <p className="mt-2 text-xs text-gray-500">
              {workspaceList.length - 1} more worktree
              {workspaceList.length - 1 === 1 ? "" : "s"} — see{" "}
              <Link to={workspaceHref(projectId, "workspaces", location.search)} className="text-indigo-400 hover:underline">
                Workspaces tab
              </Link>
              .
            </p>
          )}
        </Card>

        {/* Launch */}
        <Card title="Launch" icon={<ArrowTopRightOnSquareIcon className="h-4 w-4" />}>
          <ActionButton
            to="/agents"
            icon={<UsersIcon className="h-4 w-4" />}
            label="Open agent flock"
          />
          <ActionButton
            to={workspaceHref(projectId, "graph", location.search)}
            icon={<Squares2X2Icon className="h-4 w-4" />}
            label="Open task graph"
          />
          {discordChannelId && (
            <ExternalLink
              href={`https://discord.com/channels/@me/${discordChannelId}`}
              label="Open Discord channel"
            />
          )}
          <p className="mt-2 text-xs text-gray-500">
            Build & dev-server launch coming with per-project config.
          </p>
        </Card>
      </section>

      {/* Config strip */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">Configuration</h2>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm sm:grid-cols-3">
          <MetaField
            label="Status"
            value={
              <StatusBadge status={(project as { status?: string } | undefined)?.status ?? "—"} />
            }
          />
          <MetaField label="Credit weight" value={fmtNumber(creditWeight, "1.0")} />
          <MetaField label="Max concurrent agents" value={fmtNumber(maxAgents, "2")} />
          <MetaField label="Default profile" value={defaultProfile ?? "— fallback —"} />
          <MetaField label="Budget limit" value={fmtBudget(budget)} />
          <MetaField
            label="Tokens used"
            value={
              tokensUsed != null
                ? `${tokensUsed.toLocaleString()}${
                    tokensRecent ? ` (${tokensRecent.toLocaleString()} recent)` : ""
                  }`
                : "—"
            }
          />
        </div>
      </section>

      {/* Task status breakdown + active list */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Breakdown */}
        <section className="lg:col-span-1">
          <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">
            Task breakdown
          </h2>
          <div className="space-y-2 rounded-lg border border-gray-800 bg-gray-900 p-4">
            {Object.entries(statusCounts).length === 0 && (
              <p className="text-sm text-gray-500">No tasks yet.</p>
            )}
            {Object.entries(statusCounts)
              .sort((a, b) => b[1] - a[1])
              .map(([status, count]) => (
                <div key={status} className="flex items-center justify-between text-sm">
                  <StatusBadge status={status} />
                  <span className="font-mono text-gray-300">{count}</span>
                </div>
              ))}
            {failed > 0 && (
              <div className="mt-3 flex items-center gap-2 border-t border-gray-800 pt-3 text-xs text-red-400">
                <ExclamationTriangleIcon className="h-4 w-4" />
                <span>
                  {failed} task{failed === 1 ? "" : "s"} failed —{" "}
                  <Link to={workspaceHref(projectId, "tasks", location.search)} className="underline hover:text-red-300">
                    review
                  </Link>
                </span>
              </div>
            )}
          </div>
        </section>

        {/* Active tasks */}
        <section className="lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase text-gray-500">Active tasks</h2>
            <Link to={workspaceHref(projectId, "tasks", location.search)} className="text-xs text-indigo-400 hover:underline">
              View all →
            </Link>
          </div>
          {activeTasks.length === 0 ? (
            <div className="flex items-center gap-2 rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm text-gray-500">
              <CheckCircleIcon className="h-4 w-4 text-emerald-400" />
              <span>No active tasks — queue clear.</span>
            </div>
          ) : (
            <div className="space-y-2">
              {activeTasks.slice(0, 8).map((task) => (
                <Link
                  key={task.id}
                  to={`/tasks/${encodeURIComponent(task.id)}`}
                  state={{ from: location.pathname + location.search }}
                  className="flex items-center justify-between gap-3 rounded-lg border border-gray-800 bg-gray-900 px-4 py-3 transition-colors hover:border-indigo-500/50"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-gray-100">{task.title}</p>
                    <p className="text-xs text-gray-500">
                      {task.assigned_agent ?? "unassigned"}
                      {task.profile_id && ` · ${task.profile_id}`}
                    </p>
                  </div>
                  <StatusBadge status={task.status} />
                </Link>
              ))}
              {activeTasks.length > 8 && (
                <p className="text-xs text-gray-500">
                  +{activeTasks.length - 8} more · see{" "}
                  <Link to={workspaceHref(projectId, "tasks", location.search)} className="text-indigo-400 hover:underline">
                    Tasks tab
                  </Link>
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

// ---------- helpers ----------

function countBy<T>(items: T[], key: (t: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) {
    const k = key(it);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

function isGitHubUrl(url: string): boolean {
  return /github\.com/i.test(url);
}

function fmtNumber(v: number | undefined, fallback: string): string {
  return v != null ? String(v) : fallback;
}

function fmtBudget(v: number | null | undefined): string {
  if (v == null) return "unlimited";
  return `${v.toLocaleString()} tokens`;
}

// ---------- primitives ----------

function StatCard({
  icon,
  label,
  value,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-gray-800 bg-gray-900 p-3">
      <div className="mt-0.5">{icon}</div>
      <div className="min-w-0">
        <p className="text-xs text-gray-400">{label}</p>
        <p className="text-lg font-semibold text-gray-100">{value}</p>
        {hint && <p className="text-xs text-gray-500">{hint}</p>}
      </div>
    </div>
  );
}

function Card({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-gray-800 bg-gray-900 p-4">
      <div className="flex items-center gap-2 border-b border-gray-800 pb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
        {icon}
        <span>{title}</span>
      </div>
      {children}
    </div>
  );
}

function CopyRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable — swallow */
    }
  };
  return (
    <div className="flex items-start justify-between gap-2 text-sm">
      <div className="min-w-0 flex-1">
        <p className="text-xs text-gray-500">{label}</p>
        <p className={`truncate text-gray-300 ${mono ? "font-mono text-xs" : ""}`} title={value}>
          {value}
        </p>
      </div>
      <button
        type="button"
        onClick={onCopy}
        className="shrink-0 rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-gray-200"
        title={copied ? "Copied!" : "Copy"}
        aria-label={`Copy ${label}`}
      >
        {copied ? (
          <CheckCircleIcon className="h-4 w-4 text-emerald-400" />
        ) : (
          <ClipboardDocumentIcon className="h-4 w-4" />
        )}
      </button>
    </div>
  );
}

function ExternalLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1.5 rounded-md border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-indigo-400 hover:border-indigo-500/50 hover:text-indigo-300"
    >
      {label}
      <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
    </a>
  );
}

function ActionButton({
  to,
  icon,
  label,
}: {
  to: string;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-2 rounded-md border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-gray-200 hover:border-indigo-500/50 hover:bg-indigo-500/5 hover:text-indigo-300"
    >
      {icon}
      <span>{label}</span>
    </Link>
  );
}

function MetaField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-gray-500">{label}</p>
      <div className="mt-0.5 truncate text-gray-300">{value}</div>
    </div>
  );
}

function EmptyRow({ text }: { text: string }) {
  return <p className="text-xs italic text-gray-500">{text}</p>;
}
