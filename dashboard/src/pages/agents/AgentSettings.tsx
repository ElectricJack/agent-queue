import { useState } from "react";
import { useEditAgent, useDeleteAgent, type FlockAgent } from "../../api/agents";
import { usePoolStatus } from "../../api/hooks";
import AgentDefinitionFields, { type DefinitionForm } from "./AgentDefinitionFields";
import PoolScaleFields from "./PoolScaleFields";

export default function AgentSettings({ agent, onDeleted }: { agent: FlockAgent; onDeleted: () => void }) {
  const edit = useEditAgent();
  const remove = useDeleteAgent();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const supervisor = agent.role === "supervisor" || agent.id === "supervisor-global";
  const deleteBlocked = !!agent.current_task_id || agent.state === "busy"
    || ["starting", "running", "draining", "stopping"].includes(agent.session_state || "");
  const pending = edit.isPending || remove.isPending;
  const [draft, setDraft] = useState<DefinitionForm | null>(null);
  const [saved, setSaved] = useState(false);
  const settings = agent.settings;
  // A pool profile is sized per project, so one worker can front several
  // sets of bounds; pool_status reports one row per (project, profile).
  const poolRows = (usePoolStatus().data ?? []).filter((row) => row.profile_id === agent.profile_id);
  const baseline: DefinitionForm = {
    name: settings.name,
    profile_id: settings.profile_id,
    harness: settings.harness || "",
    model: settings.model || "",
    intelligence_class: settings.intelligence_class || "",
    enabled: settings.enabled !== false,
  };
  // Polling may update runtime fields while the user edits. Only pristine
  // forms follow incoming configuration; an in-progress draft stays intact.
  const form = draft ?? baseline;
  const dirty = JSON.stringify(form) !== JSON.stringify(baseline);

  return (
    <form aria-label={agent.name + " settings"} className="h-full space-y-4 overflow-auto p-4"
      onSubmit={(event) => {
        event.preventDefault();
        // Enter inside the pool-bounds fields below implicitly submits this
        // form; an unchanged definition must not be re-saved because of it.
        if (!dirty) return;
        edit.mutate({
          agent_id: agent.id,
          name: form.name.trim(),
          profile_id: form.profile_id,
          harness: form.harness.trim(),
          model: form.model.trim(),
          intelligence_class: form.intelligence_class.trim(),
          enabled: form.enabled,
        }, { onSuccess: () => { setDraft(null); setSaved(true); } });
      }}>
      <p className="text-xs leading-relaxed text-gray-400">
        These settings belong to this global worker. Changes apply to the next session;
        running work and shared profiles are not changed.
      </p>
      <fieldset disabled={pending}>
        <AgentDefinitionFields value={form} allowSupervisor={supervisor} onChange={(next) => { setDraft(next); setSaved(false); }} />
      </fieldset>
      {edit.error && <p role="alert" className="text-sm text-red-300">{edit.error.message}</p>}
      {saved && <p role="status" className="text-xs text-emerald-400">Saved. Changes apply to the next session.</p>}
      <div className="flex items-center gap-2">
        <button type="submit" disabled={!dirty || pending || !form.name.trim() || !form.profile_id}
          className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
          {edit.isPending ? "Saving…" : "Save settings"}
        </button>
        <button type="button" disabled={!dirty || pending}
          onClick={() => { setDraft(null); setSaved(false); edit.reset(); }}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800 disabled:opacity-40">
          Discard changes
        </button>
      </div>
      {poolRows.length > 0 && (
        <section aria-label="Worker pool settings" className="space-y-4 border-t border-gray-800 pt-4">
          <p className="text-xs leading-relaxed text-gray-400">
            Profile <span className="text-gray-200">{agent.profile_id}</span> runs as a worker pool
            (lifecycle: pool). The daemon starts and drains its sessions to stay between the bounds
            below, per project — this worker is one of them.
          </p>
          {poolRows.map((row) => (
            <div key={row.project_id + "/" + row.profile_id} className="space-y-2">
              <p className="text-xs font-medium text-gray-300">{row.project_id}</p>
              <PoolScaleFields pool={row} />
            </div>
          ))}
        </section>
      )}
      <div className="space-y-3 border-t border-gray-800 pt-4">
        {supervisor ? (
          <p className="text-xs text-gray-500">Supervisor agents cannot be deleted.</p>
        ) : (
          <>
            <p className="text-xs leading-relaxed text-gray-500">
              Delete this worker from the flock. Task and session history will be preserved.
            </p>
            {deleteBlocked && (
              <p className="text-xs text-amber-300">
                This agent has assigned work or a live session. Wait until it is idle and its session has stopped before deleting.
              </p>
            )}
            {confirmDelete ? (
              <div role="group" aria-label={"Confirm delete " + agent.name} className="space-y-3 rounded border border-red-900 bg-red-950/20 p-3">
                <p className="text-sm text-gray-300">Delete {agent.name} from the flock?</p>
                <div className="flex gap-2">
                  <button type="button" disabled={deleteBlocked || pending}
                    onClick={() => {
                      // Keep completion handling if the user switches tabs mid-request.
                      void remove.mutateAsync({ agent_id: agent.id }).then(onDeleted, () => {});
                    }}
                    className="rounded bg-red-700 px-3 py-2 text-xs text-white hover:bg-red-600 disabled:opacity-40">
                    {remove.isPending ? "Deleting…" : "Confirm delete"}
                  </button>
                  <button type="button" disabled={remove.isPending}
                    onClick={() => { setConfirmDelete(false); remove.reset(); }}
                    className="rounded px-3 py-2 text-xs text-gray-400 hover:bg-gray-800 disabled:opacity-40">
                    Cancel deletion
                  </button>
                </div>
              </div>
            ) : (
              <button type="button" disabled={deleteBlocked || pending}
                onClick={() => { remove.reset(); setConfirmDelete(true); }}
                className="rounded border border-red-900 px-3 py-2 text-xs text-red-300 hover:bg-red-950/40 disabled:opacity-40">
                Delete agent
              </button>
            )}
            {remove.error && <p role="alert" className="text-sm text-red-300">{remove.error.message}</p>}
          </>
        )}
      </div>
    </form>
  );
}
