import { ArrowPathIcon, LockClosedIcon } from "@heroicons/react/24/outline";
import {
  useMcpServers,
  useProbeMcpServer,
  type McpServerSummary,
} from "../../api/hooks";

interface Props {
  projectId: string;
  value: string[];
  onChange: (names: string[]) => void;
}

export default function McpServerSelector({ projectId, value, onChange }: Props) {
  const { data: servers, isLoading, error } = useMcpServers(projectId);
  const probe = useProbeMcpServer();

  if (isLoading) {
    return <p className="text-xs text-gray-500">Loading servers…</p>;
  }
  if (error) {
    return (
      <p className="text-xs text-red-400">
        Failed to load MCP servers: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }

  const list = servers ?? [];
  if (list.length === 0) {
    return (
      <p className="text-xs text-gray-500">
        No MCP servers registered. Define one under <span className="font-mono">~/.agent-queue/vault/mcp-servers/</span>
        {" "}or via <span className="font-mono">aq mcp create-mcp-server</span>.
      </p>
    );
  }

  const selected = new Set(value);
  const sorted = [...list].sort(serverSort);

  const toggle = (name: string) => {
    if (selected.has(name)) {
      onChange(value.filter((n) => n !== name));
    } else {
      onChange([...value, name]);
    }
  };

  const onRefresh = (name: string, scopedProjectId: string | null | undefined) => {
    probe.mutate({ name, project_id: scopedProjectId ?? undefined });
  };

  return (
    <ul className="divide-y divide-gray-800 overflow-hidden rounded-md border border-gray-800 bg-gray-950">
      {sorted.map((s) => (
        <ServerRow
          key={`${s.scope}:${s.name}`}
          server={s}
          checked={selected.has(s.name)}
          onToggle={() => toggle(s.name)}
          onRefresh={() => onRefresh(s.name, s.project_id)}
          refreshing={probe.isPending && probe.variables?.name === s.name}
        />
      ))}
    </ul>
  );
}

function ServerRow({
  server,
  checked,
  onToggle,
  onRefresh,
  refreshing,
}: {
  server: McpServerSummary;
  checked: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const builtin = !!server.is_builtin;
  const inherited = server.scope === "system";
  const toolCount = server.tool_count ?? 0;
  const lastError = server.last_error;
  const probedAgo = formatProbedAgo(server.last_probed_at ?? null);

  return (
    <li
      className={`flex items-center gap-3 px-3 py-2 ${
        inherited ? "bg-gray-950/60" : "bg-gray-900/40"
      }`}
    >
      <input
        type="checkbox"
        checked={checked || builtin}
        disabled={builtin}
        onChange={onToggle}
        className="h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-indigo-500 disabled:cursor-not-allowed"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={`truncate font-mono text-xs ${
              inherited ? "text-gray-400" : "text-gray-100"
            }`}
          >
            {server.name}
          </span>
          <ScopeBadge scope={server.scope} builtin={builtin} />
          <span className="font-mono text-[10px] uppercase tracking-wider text-gray-600">
            {server.transport}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-gray-500">
          <span>
            {toolCount} {toolCount === 1 ? "tool" : "tools"}
          </span>
          <span className="text-gray-700">·</span>
          <span title={server.last_probed_at ? new Date(server.last_probed_at * 1000).toLocaleString() : "never"}>
            {probedAgo}
          </span>
          {lastError && (
            <>
              <span className="text-gray-700">·</span>
              <span className="truncate text-amber-400" title={lastError}>
                probe failed
              </span>
            </>
          )}
        </div>
      </div>
      {!builtin ? (
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 disabled:cursor-not-allowed disabled:opacity-40"
          title="Re-probe this server"
        >
          <ArrowPathIcon className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} />
        </button>
      ) : (
        <LockClosedIcon className="h-3.5 w-3.5 text-gray-600" aria-label="Built-in (always available)" />
      )}
    </li>
  );
}

function ScopeBadge({ scope, builtin }: { scope: string; builtin: boolean }) {
  if (builtin) {
    return (
      <span className="rounded bg-emerald-500/10 px-1.5 py-px text-[10px] font-medium text-emerald-300">
        Builtin
      </span>
    );
  }
  if (scope === "project") {
    return (
      <span className="rounded bg-indigo-500/10 px-1.5 py-px text-[10px] font-medium text-indigo-300">
        Project
      </span>
    );
  }
  return (
    <span className="rounded bg-gray-800 px-1.5 py-px text-[10px] font-medium text-gray-400">
      System
    </span>
  );
}

function serverSort(a: McpServerSummary, b: McpServerSummary): number {
  const rank = (s: McpServerSummary) =>
    s.is_builtin ? 0 : s.scope === "project" ? 1 : 2;
  const ra = rank(a);
  const rb = rank(b);
  if (ra !== rb) return ra - rb;
  return a.name.localeCompare(b.name);
}

function formatProbedAgo(ts: number | null): string {
  if (!ts) return "never probed";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (seconds < 60) return "probed just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `probed ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `probed ${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `probed ${days}d ago`;
}
