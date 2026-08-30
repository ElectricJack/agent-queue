import { useState } from "react";
import { useEditAgent, type FlockAgent } from "../../api/agents";
import AgentDefinitionFields, { type DefinitionForm } from "./AgentDefinitionFields";

export default function AgentSettings({ agent }: { agent: FlockAgent }) {
  const edit = useEditAgent();
  const [draft, setDraft] = useState<DefinitionForm | null>(null);
  const [saved, setSaved] = useState(false);
  const settings = agent.settings;
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
      <fieldset disabled={edit.isPending}>
        <AgentDefinitionFields value={form} onChange={(next) => { setDraft(next); setSaved(false); }} />
      </fieldset>
      {edit.error && <p role="alert" className="text-sm text-red-300">{edit.error.message}</p>}
      {saved && <p role="status" className="text-xs text-emerald-400">Saved. Changes apply to the next session.</p>}
      <div className="flex items-center gap-2">
        <button type="submit" disabled={!dirty || edit.isPending || !form.name.trim() || !form.profile_id}
          className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
          {edit.isPending ? "Saving…" : "Save settings"}
        </button>
        <button type="button" disabled={!dirty || edit.isPending}
          onClick={() => { setDraft(null); setSaved(false); edit.reset(); }}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800 disabled:opacity-40">
          Discard changes
        </button>
      </div>
    </form>
  );
}
