import { useEffect, useState } from "react";
import { useProjects } from "../../api/hooks";
import { useProjectGraphs } from "../../api/graph";
import GraphCanvas from "./GraphCanvas";
import ProjectStrip from "./ProjectStrip";
import GhostOverlay from "./GhostOverlay";
import MobileCardList from "./MobileCardList";
import { useGraphLive } from "./useGraphLive";
import { useShellPaneStore } from "../../panes/store";

const SELECTED_KEY = "aq:command-center:selected";

function useIsMobile() {
  const [m, setM] = useState(() => window.matchMedia("(max-width: 768px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const onC = (e: MediaQueryListEvent) => setM(e.matches);
    mq.addEventListener("change", onC);
    return () => mq.removeEventListener("change", onC);
  }, []);
  return m;
}

function useIsLandscape() {
  const [l, setL] = useState(() =>
    window.matchMedia("(orientation: landscape)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(orientation: landscape)");
    const onC = (e: MediaQueryListEvent) => setL(e.matches);
    mq.addEventListener("change", onC);
    return () => mq.removeEventListener("change", onC);
  }, []);
  return l;
}

export default function CommandCenterGraph() {
  const { data: projects = [] } = useProjects();
  const [selected, setSelected] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(SELECTED_KEY) ?? "[]");
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(SELECTED_KEY, JSON.stringify(selected));
    } catch {
      /* ignore */
    }
  }, [selected]);
  // Drop persisted ids for projects that no longer exist. Without this a
  // deleted project stays in localStorage forever, and every visit pays a
  // failed graph fetch for it. Only prunes once the project list has actually
  // loaded, so an empty list mid-fetch can't wipe a valid selection.
  useEffect(() => {
    if (projects.length === 0) return;
    const live = new Set(projects.map((p) => p.id));
    setSelected((prev) => {
      const kept = prev.filter((id) => live.has(id));
      return kept.length === prev.length ? prev : kept;
    });
  }, [projects]);

  useEffect(() => {
    if (selected.length === 0 && projects.length > 0 && projects[0]) {
      setSelected([projects[0].id]);
    }
  }, [projects, selected.length]);

  const { data: graph, isLoading } = useProjectGraphs(selected);
  useGraphLive(selected);

  const isMobile = useIsMobile();
  const isLandscape = useIsLandscape();
  const showCanvas = !isMobile || isLandscape;

  const pane = useShellPaneStore();

  const toggle = (pid: string) =>
    setSelected((prev) =>
      prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid],
    );

  return (
    <div className="flex h-full flex-col">
      <ProjectStrip
        projects={projects}
        graph={graph}
        selected={selected}
        onToggle={toggle}
      />
      <div className="relative flex-1 overflow-hidden">
        {isLoading ? (
          <p className="p-4 text-sm text-gray-400">Loading graph…</p>
        ) : showCanvas ? (
          <>
            <GraphCanvas
              graph={graph}
              onTaskClick={(taskId) => pane.open("task-detail", { taskId })}
            />
            <GhostOverlay proposalId={null} />
          </>
        ) : (
          <MobileCardList
            graph={graph}
            onTaskClick={(taskId) => pane.open("task-detail", { taskId })}
          />
        )}
      </div>
    </div>
  );
}
