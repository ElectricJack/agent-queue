import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  PencilSquareIcon,
  PlusIcon,
  ArrowUturnLeftIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import {
  useCreateProjectProfile,
  useProjectProfiles,
  type ProfileDetail,
  type ProjectProfileRow,
} from "../../api/hooks";
import ProfileEditDrawer from "../../components/profile/ProfileEditDrawer";
import DeleteProjectProfileModal from "../../components/profile/DeleteProjectProfileModal";
import CreateProjectProfileModal from "../../components/profile/CreateProjectProfileModal";

export default function ProjectProfiles() {
  const { projectId = "" } = useParams();
  const { data, isLoading, error } = useProjectProfiles(projectId);
  const create = useCreateProjectProfile();
  const [editingType, setEditingType] = useState<string | null>(null);
  const [resetting, setResetting] = useState<{ agent_type: string; hasGlobal: boolean } | null>(
    null,
  );
  const [createError, setCreateError] = useState<string | null>(null);
  const [pendingType, setPendingType] = useState<string | null>(null);
  const [creatingNew, setCreatingNew] = useState(false);

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (error) {
    return (
      <p className="text-sm text-red-400">
        Failed to load profiles: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }

  const rows = data?.agent_types ?? [];

  const onAddOverride = async (row: ProjectProfileRow) => {
    setCreateError(null);
    setPendingType(row.agent_type);
    try {
      await create.mutateAsync({
        project_id: projectId,
        agent_type: row.agent_type,
        seed_from_global: !!row.global,
      });
      setEditingType(row.agent_type);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setPendingType(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <p className="max-w-2xl text-xs text-gray-500">
          One row per agent type. Project overrides take precedence over the global default for
          tasks in this project. Reset to global to remove an override.
        </p>
        <button
          onClick={() => setCreatingNew(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
        >
          <PlusIcon className="h-3.5 w-3.5" />
          New profile
        </button>
      </div>

      {createError && (
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{createError}</span>
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-sm text-gray-500">No agent types configured.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-800 bg-gray-900/50 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-3">Agent type</th>
                <th className="px-4 py-3">Source</th>
                <th className="px-4 py-3">Effective profile</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {rows.map((row) => (
                <ProfileRow
                  key={row.agent_type}
                  row={row}
                  pending={pendingType === row.agent_type}
                  onEdit={() => setEditingType(row.agent_type)}
                  onAddOverride={() => onAddOverride(row)}
                  onReset={() =>
                    setResetting({ agent_type: row.agent_type, hasGlobal: !!row.global })
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editingType && (
        <ProfileEditDrawer
          open
          onClose={() => setEditingType(null)}
          projectId={projectId}
          agentType={editingType}
        />
      )}

      {resetting && (
        <DeleteProjectProfileModal
          open
          onClose={() => setResetting(null)}
          projectId={projectId}
          agentType={resetting.agent_type}
          hasGlobal={resetting.hasGlobal}
        />
      )}

      <CreateProjectProfileModal
        open={creatingNew}
        onClose={() => setCreatingNew(false)}
        projectId={projectId}
        existingAgentTypes={rows.map((r) => r.agent_type)}
        onCreated={(agentType) => setEditingType(agentType)}
      />
    </div>
  );
}

interface RowProps {
  row: ProjectProfileRow;
  pending: boolean;
  onEdit: () => void;
  onAddOverride: () => void;
  onReset: () => void;
}

function ProfileRow({ row, pending, onEdit, onAddOverride, onReset }: RowProps) {
  const hasScoped = !!row.scoped;
  const hasGlobal = !!row.global;
  const effective = row.effective ?? row.scoped ?? row.global ?? null;

  return (
    <tr className="hover:bg-gray-900/50">
      <td className="px-4 py-3 align-top">
        <div className="font-medium text-gray-100">{row.agent_type}</div>
        {effective?.name && effective.name !== row.agent_type && (
          <div className="text-xs text-gray-500">{effective.name}</div>
        )}
      </td>
      <td className="px-4 py-3 align-top">
        <SourceBadge hasScoped={hasScoped} hasGlobal={hasGlobal} />
      </td>
      <td className="px-4 py-3 align-top">
        <ProfileChips profile={effective} />
      </td>
      <td className="px-4 py-3 align-top text-right">
        <div className="inline-flex items-center gap-1">
          {hasScoped ? (
            <>
              <ActionButton onClick={onEdit} icon={PencilSquareIcon} label="Edit" />
              <ActionButton
                onClick={onReset}
                icon={ArrowUturnLeftIcon}
                label="Reset to global"
                variant="amber"
              />
            </>
          ) : (
            <ActionButton
              onClick={onAddOverride}
              icon={PlusIcon}
              label={pending ? "Creating..." : "Add project override"}
              disabled={pending}
              variant="indigo"
            />
          )}
        </div>
      </td>
    </tr>
  );
}

function SourceBadge({ hasScoped, hasGlobal }: { hasScoped: boolean; hasGlobal: boolean }) {
  if (hasScoped) {
    return (
      <span className="rounded-full bg-indigo-500/10 px-2 py-0.5 text-xs font-medium text-indigo-300">
        Project override
      </span>
    );
  }
  if (hasGlobal) {
    return (
      <span className="rounded-full bg-gray-800 px-2 py-0.5 text-xs font-medium text-gray-400">
        Inherits global
      </span>
    );
  }
  return (
    <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-300">
      No global default
    </span>
  );
}

function ProfileChips({ profile }: { profile: ProfileDetail | null }) {
  if (!profile) return <span className="text-xs text-gray-600">—</span>;
  const toolCount = (profile.allowed_tools ?? []).length;
  const mcpCount = (profile.mcp_servers ?? []).length;
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      {profile.model && (
        <span className="rounded bg-gray-800 px-2 py-0.5 font-mono text-gray-300">
          {profile.model}
        </span>
      )}
      <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-400">
        {toolCount} {toolCount === 1 ? "tool" : "tools"}
      </span>
      <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-400">
        {mcpCount} MCP
      </span>
      {profile.system_prompt_suffix && (
        <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-500">custom prompt</span>
      )}
    </div>
  );
}

interface ActionButtonProps {
  onClick: () => void;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  disabled?: boolean;
  variant?: "default" | "indigo" | "amber";
}

function ActionButton({
  onClick,
  icon: Icon,
  label,
  disabled,
  variant = "default",
}: ActionButtonProps) {
  const styles = {
    default: "bg-gray-800 text-gray-200 hover:bg-gray-700",
    indigo: "bg-indigo-600/90 text-white hover:bg-indigo-500",
    amber: "bg-amber-600/20 text-amber-200 hover:bg-amber-600/30",
  }[variant];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}
