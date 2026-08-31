import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useIsMutating } from "@tanstack/react-query";
import { usePlaybooks, usePlaybookRuns, useRunPlaybook, useSetPlaybookEnabled, type PlaybookSummary } from "../../api/hooks";
import { useEventStream } from "../../ws/useEventStream";
import { manualPlaybookEvent, playbookRunning, playbookScope, playbookState } from "../../pages/command-center/playbooks";
import { useShellPaneStore } from "../store";
import type { PaneViewProps } from "../types";
import type { PlaybookDetailArgs } from "./manifest";

export default function PlaybookDetailPane({ args, close }: PaneViewProps<PlaybookDetailArgs>) {
  const { data, isLoading, error, refetch } = usePlaybooks();
  useEventStream();
  const playbook = data?.find(p => p.id === args.playbookId);
  if (isLoading) return <p className="p-4 text-sm text-gray-400">Loading playbook…</p>;
  if (error) return <p role="alert" className="p-4 text-sm text-red-300">Could not load playbook. <button onClick={() => refetch()}>Retry</button></p>;
  if (!playbook) return <p className="p-4 text-sm text-gray-400">This playbook is no longer available.</p>;
  return <Definition key={playbook.id} playbook={playbook} close={close} />;
}

function Definition({ playbook: p, close }: { playbook: PlaybookSummary; close: () => void }) {
  const location = useLocation();
  const { open } = useShellPaneStore();
  const { data: runs, isLoading, error, refetch } = usePlaybookRuns(p.id);
  const run = useRunPlaybook();
  const toggle = useSetPlaybookEnabled();
  const pending = useIsMutating({ mutationKey: ["run-playbook"], predicate: m => (m.state.variables as { playbook_id?: string } | undefined)?.playbook_id === p.id }) > 0;
  const [eventText, setEventText] = useState('{"type":"manual"}');
  const [runForm, setRunForm] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const active = playbookRunning(p);
  const earlierPaused = (runs ?? []).filter(r => r.status === "paused" && r.run_id !== p.last_run?.run_id).length;
  const disabled = p.enabled === false || active || pending || run.isPending || toggle.isPending;
  const start = () => {
    setFormError(null);
    try {
      const event = manualPlaybookEvent(p, eventText);
      run.mutate({ playbook_id: p.id, event }, { onSuccess: result => {
        if (result.error) setFormError(result.error);
        else setRunForm(false);
      } });
    } catch (err) { setFormError(err instanceof Error ? err.message : String(err)); }
  };
  const buttonClass = "rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50";
  return <div className="space-y-4 p-4 text-sm text-gray-300">
    <div><h2 className="break-words text-lg font-semibold text-white">{p.id}</h2><p className="mt-1 text-violet-300">{playbookState(p)}</p><p className="mt-1 text-xs text-gray-400">{playbookScope(p)} · v{p.version ?? 0} · {p.node_count ?? 0} nodes</p></div>
    <p className="text-xs text-gray-400">This recurring definition stays in the graph after each run. Completed runs remain in its history.</p>
    <div><h3 className="font-medium">Triggers</h3><p className="mt-1 break-words font-mono text-xs">{[...new Set(p.triggers ?? [])].join(", ") || "Manual only"}</p>{(p.cooldown_remaining ?? 0) > 0 && <p className="mt-1 text-xs text-gray-400">Cooldown: {Math.ceil(p.cooldown_remaining!)}s remaining</p>}</div>
    <div className="flex flex-wrap gap-2">
      <button className={buttonClass} disabled={disabled} onClick={() => setRunForm(!runForm)}>{pending || run.isPending ? "Running…" : p.last_run ? "Run again" : "Run now"}</button>
      <button className={buttonClass} disabled={toggle.isPending} onClick={() => toggle.mutate({ playbook_id: p.id, enabled: p.enabled === false })}>{p.enabled === false ? "Resume triggers" : "Pause triggers"}</button>
      <Link className={buttonClass} to={`/playbooks/${encodeURIComponent(p.id)}`} state={{ from: `${location.pathname}${location.search}` }} onClick={close}>Edit definition</Link>
    </div>
    {p.enabled === false && <p className="text-xs text-amber-300">Triggers are paused. Existing runs continue; resume triggers before starting another run.</p>}
    {earlierPaused > 0 && <p className="text-xs text-amber-300">{earlierPaused} earlier runs in this history are paused. Starting a new run does not resume or cancel them; inspect them below.</p>}
    {runForm && <form className="space-y-2 rounded border border-gray-700 p-3" onSubmit={e => { e.preventDefault(); if (!disabled) start(); }}>
      <label className="block text-xs">Trigger event (JSON)<textarea aria-label="Trigger event (JSON)" value={eventText} onChange={e => setEventText(e.target.value)} className="mt-2 h-28 w-full rounded bg-gray-950 p-2 font-mono text-xs" /></label>
      <p className="text-xs text-gray-400">Add any task or event fields this playbook expects. Starting runs executes the playbook's actions. Project scope is preserved.</p>
      <button type="submit" className={buttonClass} disabled={disabled}>Start run</button>
    </form>}
    {(formError || run.error || toggle.error) && <p role="alert" className="text-red-300">{formError || String(run.error || toggle.error)}</p>}
    <div><h3 className="font-medium">Recent runs</h3>
      {isLoading && <p className="mt-2 text-gray-400">Loading runs…</p>}
      {error && <p role="alert" className="mt-2 text-red-300">Could not load runs. <button onClick={() => refetch()}>Retry runs</button></p>}
      {!isLoading && !error && !runs?.length && <p className="mt-2 text-gray-400">No runs yet.</p>}
      <ul className="mt-2 space-y-2">{runs?.map(r => <li key={r.run_id}><button className="w-full rounded border border-gray-800 p-2 text-left hover:bg-gray-900" onClick={() => open("playbook-run-inspector", { runId: r.run_id })}>
        <span className="flex justify-between gap-2"><span className="font-mono text-xs">{r.run_id.slice(0, 12)}</span><span>{r.status.replace(/_/g, " ")}</span></span>
        <span className="mt-1 block text-xs text-gray-400">{r.started_at ? new Date(r.started_at * 1000).toLocaleString() : "Start time unknown"}{r.current_node ? ` · ${r.current_node}` : ""}</span>
      </button></li>)}</ul>
    </div>
  </div>;
}
