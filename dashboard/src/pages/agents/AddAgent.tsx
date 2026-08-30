import { useState } from "react";
import { useCreateAgent } from "../../api/agents";
import AgentDefinitionFields, { type DefinitionForm } from "./AgentDefinitionFields";

export default function AddAgent({ onCreated, onCancel }: {
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const create = useCreateAgent();
  const [form, setForm] = useState<DefinitionForm>({
    name: "", profile_id: "", harness: "", model: "", intelligence_class: "", enabled: true,
  });

  return (
    <form aria-label="Add agent" className="max-h-[65vh] shrink-0 space-y-4 overflow-auto rounded-xl border border-gray-700 bg-gray-900 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate({ ...form, name: form.name.trim(), model: form.model.trim(),
          harness: form.harness.trim(), intelligence_class: form.intelligence_class.trim() },
        { onSuccess: (agent) => onCreated(agent.id) });
      }}>
      <p className="text-sm text-gray-400">Define a shared worker. A session starts only when it receives work.</p>
      <fieldset disabled={create.isPending}>
        <AgentDefinitionFields value={form} onChange={setForm} />
      </fieldset>
      {create.error && <p role="alert" className="text-sm text-red-300">{create.error.message}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={create.isPending || !form.name.trim() || !form.profile_id}
          className="rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-40">
          {create.isPending ? "Creating…" : "Create agent"}
        </button>
        <button type="button" disabled={create.isPending} onClick={onCancel}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800">Cancel</button>
      </div>
    </form>
  );
}
