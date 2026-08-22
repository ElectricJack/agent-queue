import { useEffect, useMemo, useState } from "react";
import { useProjects } from "../api/hooks";
import { useResolveGate } from "../api/hooks";
import { useProjectGraphs } from "../api/graph";
import GraphCanvas from "./command-center/GraphCanvas";
import ProjectStrip from "./command-center/ProjectStrip";
import TaskSidebar from "./command-center/TaskSidebar";
import GhostOverlay from "./command-center/GhostOverlay";
import MobileCardList from "./command-center/MobileCardList";
import { useGraphLive } from "./command-center/useGraphLive";

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

export default function CommandCenter() {
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

  // Auto-select first project if none.
  useEffect(() => {
    if (selected.length === 0 && projects.length > 0 && projects[0]) {
      setSelected([projects[0].id]);
    }
  }, [projects, selected.length]);

  const { data: graph, isLoading } = useProjectGraphs(selected);
  useGraphLive(selected);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const isMobile = useIsMobile();
  const isLandscape = useIsLandscape();
  const showCanvas = !isMobile || isLandscape;

  const resolveMut = useResolveGate();

  const toggle = (pid: string) =>
    setSelected((prev) =>
      prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid],
    );

  const proposalId = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("proposal");
  }, []);

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
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
            <GraphCanvas graph={graph} onTaskClick={setSelectedTaskId} />
            <GhostOverlay proposalId={proposalId} />
          </>
        ) : (
          <MobileCardList graph={graph} onTaskClick={setSelectedTaskId} />
        )}
      </div>
      <TaskSidebar
        taskId={selectedTaskId}
        gates={graph.gates}
        onResolveGate={(gid, dec) =>
          resolveMut.mutate({
            gate_id: gid,
            resolved_by: "dashboard",
            resolution: dec,
          })
        }
        onClose={() => setSelectedTaskId(null)}
      />
    </div>
  );
}
