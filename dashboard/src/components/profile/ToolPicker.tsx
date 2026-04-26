import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowPathIcon,
  CheckIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import {
  useProbeMcpServer,
  useToolCatalog,
  type CatalogEntry,
  type ProbedTool,
} from "../../api/hooks";

interface Props {
  projectId: string;
  value: string[];
  onChange: (tools: string[]) => void;
  enabledServers: string[];
  /**
   * Profile's model. Determines whether Claude Code's built-in tools
   * (Read/Edit/Bash/etc.) apply — they only exist when the agent runs
   * through the Claude CLI adapter. Empty / non-claude-* models use the
   * chat provider and don't get them.
   */
  model: string;
}

interface Group {
  key: string;
  label: string;
  kind: "claude" | "builtin-mcp" | "mcp";
  serverName: string | null;
  serverProjectId: string | null | undefined;
  tools: ToolEntry[];
}

interface ToolEntry {
  toolName: string;
  description: string | null;
}

const CLAUDE_CODE_TOOLS: ToolEntry[] = [
  { toolName: "Read", description: "Read file contents" },
  { toolName: "Write", description: "Write/create files" },
  { toolName: "Edit", description: "Edit existing files" },
  { toolName: "Bash", description: "Run shell commands" },
  { toolName: "Glob", description: "Find files by pattern" },
  { toolName: "Grep", description: "Search file contents" },
  { toolName: "WebSearch", description: "Search the web" },
  { toolName: "WebFetch", description: "Fetch and process URL content" },
  { toolName: "NotebookEdit", description: "Edit Jupyter notebooks" },
  { toolName: "Agent", description: "Launch sub-agents" },
  { toolName: "TodoRead", description: "Read task list" },
  { toolName: "TodoWrite", description: "Write to task list" },
  { toolName: "Skill", description: "Execute a skill/slash command" },
  { toolName: "TaskCreate", description: "Create tracked tasks" },
  { toolName: "TaskUpdate", description: "Update tracked tasks" },
  { toolName: "TaskList", description: "List tracked tasks" },
  { toolName: "TaskGet", description: "Get task details" },
  { toolName: "EnterWorktree", description: "Create isolated git worktree" },
];

export default function ToolPicker({
  projectId,
  value,
  onChange,
  enabledServers,
  model,
}: Props) {
  const { data: catalog, isLoading, error } = useToolCatalog(projectId);
  const probe = useProbeMcpServer();
  const [query, setQuery] = useState("");

  const usesClaudeCode = isClaudeCodeModel(model);
  const rawGroups = useMemo<Group[]>(
    () => buildGroups(catalog ?? {}, enabledServers, usesClaudeCode),
    [catalog, enabledServers, usesClaudeCode],
  );

  // Snapshot the selection at the moment the group structure changes (catalog
  // refresh, server toggle, drawer open) and use it to pin selected tools to
  // the top of each group. Re-checking inside the picker won't reshuffle.
  const pinnedRef = useRef<Set<string>>(new Set(value));
  useEffect(() => {
    pinnedRef.current = new Set(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawGroups]);

  const groups = useMemo(
    () => sortGroupsBySelection(rawGroups, pinnedRef.current),
    [rawGroups],
  );
  const filteredGroups = useMemo(() => filterGroups(groups, query), [groups, query]);

  const selected = useMemo(() => new Set(value), [value]);

  const toggle = (tool: string) => {
    if (selected.has(tool)) {
      onChange(value.filter((t) => t !== tool));
    } else {
      onChange([...value, tool]);
    }
  };

  const setMany = (tools: string[], on: boolean) => {
    const next = new Set(value);
    for (const t of tools) {
      if (on) next.add(t);
      else next.delete(t);
    }
    onChange([...next]);
  };

  const onRefresh = (group: Group) => {
    if (!group.serverName) return;
    probe.mutate({ name: group.serverName, project_id: group.serverProjectId ?? undefined });
  };

  return (
    <div className="space-y-3">
      <div className="relative">
        <MagnifyingGlassIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter tools…"
          className="w-full rounded-md border border-gray-700 bg-gray-950 py-1.5 pl-8 pr-8 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery("")}
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-gray-500 hover:bg-gray-800 hover:text-gray-300"
          >
            <XMarkIcon className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {isLoading && <p className="text-xs text-gray-500">Loading tool catalog…</p>}
      {error && (
        <p className="text-xs text-red-400">
          Failed to load catalog: {error instanceof Error ? error.message : String(error)}
        </p>
      )}

      <div className="space-y-3">
        {filteredGroups.length === 0 && !isLoading ? (
          <p className="text-xs text-gray-500">No tools match.</p>
        ) : (
          filteredGroups.map((g) => {
            const visibleNames = g.tools.map((t) => t.toolName);
            const visibleSelected = visibleNames.filter((n) => selected.has(n)).length;
            const allOn = visibleSelected === visibleNames.length && visibleNames.length > 0;
            const refreshing =
              probe.isPending && g.serverName !== null && probe.variables?.name === g.serverName;
            return (
              <div key={g.key} className="rounded-md border border-gray-800 bg-gray-950">
                <div className="flex items-center justify-between gap-2 border-b border-gray-800 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-gray-200">{g.label}</span>
                    <span className="text-[11px] text-gray-500">
                      {visibleSelected}/{visibleNames.length}
                    </span>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => setMany(visibleNames, !allOn)}
                      className="rounded px-2 py-0.5 text-[11px] text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                    >
                      {allOn ? "Clear all" : "Select all"}
                    </button>
                    {g.kind === "mcp" && (
                      <button
                        type="button"
                        onClick={() => onRefresh(g)}
                        disabled={refreshing}
                        title="Re-probe this server"
                        className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <ArrowPathIcon
                          className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`}
                        />
                      </button>
                    )}
                  </div>
                </div>
                <ul className="divide-y divide-gray-800/60">
                  {g.tools.map((t) => {
                    const on = selected.has(t.toolName);
                    return (
                      <li
                        key={t.toolName}
                        onClick={() => toggle(t.toolName)}
                        className="flex cursor-pointer items-start gap-2 px-3 py-1.5 hover:bg-gray-900/60"
                      >
                        <span
                          className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                            on
                              ? "border-indigo-500 bg-indigo-500"
                              : "border-gray-700 bg-gray-900"
                          }`}
                        >
                          {on && <CheckIcon className="h-3 w-3 text-white" />}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="font-mono text-xs text-gray-200">{t.toolName}</div>
                          {t.description && (
                            <div className="mt-0.5 truncate text-[11px] text-gray-500">
                              {t.description}
                            </div>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })
        )}
      </div>

      <UnknownTools value={value} groups={groups} onRemove={(t) => toggle(t)} />
    </div>
  );
}

function isClaudeCodeModel(model: string): boolean {
  return model.trim().toLowerCase().startsWith("claude");
}

function buildGroups(
  catalog: Record<string, CatalogEntry>,
  enabledServers: string[],
  includeClaudeCodeTools: boolean,
): Group[] {
  const groups: Group[] = [];
  if (includeClaudeCodeTools) {
    groups.push({
      key: "claude",
      label: "Claude Code (built-in)",
      kind: "claude",
      serverName: null,
      serverProjectId: null,
      tools: CLAUDE_CODE_TOOLS,
    });
  }

  // Always include the embedded agent-queue server (auto-injected at task launch).
  const embeddedKey = Object.keys(catalog).find((k) => catalog[k]?.is_builtin) ?? "agent-queue";
  const embedded = catalog[embeddedKey];
  if (embedded) {
    groups.push({
      key: `mcp:${embedded.server_name}`,
      label: `${embedded.server_name} (embedded)`,
      kind: "builtin-mcp",
      serverName: embedded.server_name,
      serverProjectId: embedded.project_id ?? null,
      tools: probedToolsToEntries(embedded.server_name, embedded.tools ?? []),
    });
  }

  // Other enabled servers, in alphabetical order.
  const builtinName = embedded?.server_name;
  const extras = enabledServers
    .filter((n) => n !== builtinName)
    .sort((a, b) => a.localeCompare(b));
  for (const name of extras) {
    const entry = catalog[name];
    if (!entry) {
      groups.push({
        key: `mcp:${name}`,
        label: `${name} (not registered)`,
        kind: "mcp",
        serverName: name,
        serverProjectId: null,
        tools: [],
      });
      continue;
    }
    groups.push({
      key: `mcp:${name}`,
      label: name,
      kind: "mcp",
      serverName: name,
      serverProjectId: entry.project_id ?? null,
      tools: probedToolsToEntries(name, entry.tools ?? []),
    });
  }

  return groups;
}

function probedToolsToEntries(serverName: string, tools: ProbedTool[]): ToolEntry[] {
  return tools.map((t) => ({
    toolName: `mcp__${serverName}__${t.name}`,
    description: t.description ?? null,
  }));
}

function sortGroupsBySelection(groups: Group[], pinned: Set<string>): Group[] {
  return groups.map((g) => ({
    ...g,
    tools: [...g.tools].sort((a, b) => {
      const aSel = pinned.has(a.toolName);
      const bSel = pinned.has(b.toolName);
      if (aSel !== bSel) return aSel ? -1 : 1;
      return a.toolName.localeCompare(b.toolName);
    }),
  }));
}

function filterGroups(groups: Group[], query: string): Group[] {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  return groups
    .map((g) => ({
      ...g,
      tools: g.tools.filter(
        (t) =>
          t.toolName.toLowerCase().includes(q) ||
          (t.description ?? "").toLowerCase().includes(q),
      ),
    }))
    .filter((g) => g.tools.length > 0);
}

function UnknownTools({
  value,
  groups,
  onRemove,
}: {
  value: string[];
  groups: Group[];
  onRemove: (tool: string) => void;
}) {
  const known = new Set<string>();
  for (const g of groups) for (const t of g.tools) known.add(t.toolName);
  const unknown = value.filter((t) => !known.has(t));
  if (unknown.length === 0) return null;
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2">
      <p className="mb-1 text-[11px] font-medium uppercase tracking-wider text-amber-300">
        Selected tools not in catalog
      </p>
      <p className="mb-2 text-[11px] text-amber-300/80">
        These tools are saved in the profile but their server isn't enabled or registered.
      </p>
      <ul className="flex flex-wrap gap-1">
        {unknown.map((t) => (
          <li
            key={t}
            className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[11px] text-amber-200"
          >
            {t}
            <button
              type="button"
              onClick={() => onRemove(t)}
              className="rounded p-0.5 hover:bg-amber-500/20"
              title="Remove"
            >
              <XMarkIcon className="h-3 w-3" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
