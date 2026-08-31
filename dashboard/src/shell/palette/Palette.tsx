import { Command } from "cmdk";
import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useShortcut } from "../hotkeys/useShortcuts";
import { useActions } from "./registerActions";
import { usePaletteState } from "./paletteState";
import { useProjects, useActiveTasksAllProjects } from "../../api/hooks";
import { useShellPaneStore } from "../../panes/store";
import { workspaceHref, workspaceNavigation } from "../projectNavigation";

/**
 * Linear-style command palette.
 *   $mod-K       — toggle
 *   >query       — actions only
 *   #query       — tasks only
 *   @query       — projects only
 *   <no prefix>  — actions (default)
 */
export function Palette() {
  const { open, setOpen, toggle } = usePaletteState();
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  const location = useLocation();
  const pane = useShellPaneStore();
  const workspace = workspaceNavigation(location);
  const from = workspace.isWorkspace
    ? workspaceHref(workspace.projectId, workspace.tab, workspace.search)
    : (location.state as { from?: string } | null)?.from ?? location.pathname + location.search;

  useShortcut("$mod-k", {
    label: "toggle command palette",
    section: "Palette",
    onFire: toggle,
  });
  useShortcut("Escape", {
    label: "close palette",
    section: "Palette",
    onFire: () => setOpen(false),
    when: () => open,
  });

  const actions = useActions();
  const { data: projects } = useProjects();
  const { data: tasks } = useActiveTasksAllProjects();

  const prefix = q[0];
  const body = q.slice(prefix === ">" || prefix === "#" || prefix === "@" ? 1 : 0).trim().toLowerCase();

  const showActions = !prefix || prefix === ">";
  const showTasks = prefix === "#";
  const showProjects = prefix === "@";

  const matchLabel = (label: string) => label.toLowerCase().includes(body);

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Command palette"
      shouldFilter={false}
      overlayClassName="fixed inset-0 z-40 bg-black/60"
      contentClassName="fixed left-1/2 top-24 z-50 w-full max-w-xl -translate-x-1/2 overflow-hidden rounded-lg border border-gray-700 bg-gray-900 text-gray-100 shadow-xl"
    >
      <>
        <Command.Input
          value={q}
          onValueChange={setQ}
          placeholder="Type a command, #task, @project, or >action…"
          className="w-full border-b border-gray-800 bg-transparent px-4 py-3 text-sm outline-none"
        />
        <Command.List className="max-h-[50vh] overflow-y-auto p-2">
          <Command.Empty className="p-4 text-center text-xs text-gray-500">
            No results.
          </Command.Empty>

          {showActions &&
            actions
              .filter((a) => matchLabel(a.label))
              .map((a) => (
                <Command.Item
                  key={a.id}
                  value={a.id}
                  onSelect={() => {
                    a.run();
                    setOpen(false);
                    setQ("");
                  }}
                  className="cursor-pointer rounded px-3 py-2 text-sm data-[selected=true]:bg-indigo-500/20"
                >
                  {a.label}
                </Command.Item>
              ))}

          {showTasks &&
            (tasks ?? [])
              .filter((t) => matchLabel(t.title ?? ""))
              .slice(0, 25)
              .map((t) => (
                <Command.Item
                  key={t.id}
                  value={t.id}
                  onSelect={() => {
                    navigate(`/tasks/${encodeURIComponent(t.id)}`, {
                      state: { from },
                    });
                    pane.close();
                    setOpen(false);
                    setQ("");
                  }}
                  className="cursor-pointer rounded px-3 py-2 text-sm data-[selected=true]:bg-indigo-500/20"
                >
                  <span className="text-gray-200">{t.title}</span>
                  <span className="ml-2 text-xs text-gray-500">{t.project_id}</span>
                </Command.Item>
              ))}

          {showProjects &&
            (projects ?? [])
              .filter((p) => matchLabel(p.name ?? p.id))
              .map((p) => (
                <Command.Item
                  key={p.id}
                  value={p.id}
                  onSelect={() => {
                    navigate(workspaceHref(p.id, workspace.tab, workspace.search));
                    setOpen(false);
                    setQ("");
                  }}
                  className="cursor-pointer rounded px-3 py-2 text-sm data-[selected=true]:bg-indigo-500/20"
                >
                  {p.name ?? p.id}
                </Command.Item>
              ))}
        </Command.List>
      </>
    </Command.Dialog>
  );
}
