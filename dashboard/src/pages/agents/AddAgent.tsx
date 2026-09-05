import { useState } from "react";
import { useCreateAgent } from "../../api/agents";
import { useProfiles, usePoolStatus } from "../../api/hooks";
import AgentDefinitionFields, { type DefinitionForm } from "./AgentDefinitionFields";
import { poolProfileIds } from "./pools";

export default function AddAgent({ onCreated, onCancel, onSwitchToPool }: {
  onCreated: (id: string) => void;
  onCancel: () => void;
  onSwitchToPool: () => void;
}) {
  const create = useCreateAgent();
  const { data: profiles = [] } = useProfiles();
  const { data: pools = [] } = usePoolStatus();
  const [form, setForm] = useState<DefinitionForm>({
    name: "", profile_id: "", harness: "", model: "", intelligence_class: "", enabled: true,
  });
  // The picker already disables pool profiles, but a profile's lifecycle can
  // flip under a form that is already open. Refusing before submit is the
  // difference between a visible error and a worker that quietly becomes a
  // pool instance the flock then files under its pool entry.
  const poolProfile = !!form.profile_id && poolProfileIds(pools, profiles).has(form.profile_id);

  return (
    <form aria-label="Create agent" className="max-h-[65vh] shrink-0 space-y-4 overflow-auto rounded-xl border border-gray-700 bg-gray-900 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (poolProfile) return;
        create.mutate({ ...form, name: form.name.trim(), model: form.model.trim(),
          harness: form.harness.trim(), intelligence_class: form.intelligence_class.trim() },
        { onSuccess: (agent) => onCreated(agent.id) });
      }}>
      <p className="text-sm text-gray-400">
        One durable worker, shared by every project. A session starts only when it receives work.
        For elastic per-project capacity, create an agent pool instead.
      </p>
      <fieldset disabled={create.isPending}>
        <AgentDefinitionFields value={form} onChange={setForm} allowPoolProfiles={false} />
      </fieldset>
      {poolProfile && (
        <p role="alert" className="text-sm text-amber-300">
          {form.profile_id} is a pool profile: it defines elastic capacity, not a durable worker.{" "}
          <button type="button" onClick={onSwitchToPool} className="underline hover:text-amber-200">
            Create an agent pool
          </button>{" "}
          with it instead, or choose a task-lifecycle profile.
        </p>
      )}
      {create.error && <p role="alert" className="text-sm text-red-300">{create.error.message}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={create.isPending || !form.name.trim() || !form.profile_id || poolProfile}
          className="rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-40">
          {create.isPending ? "Creating…" : "Create agent"}
        </button>
        <button type="button" disabled={create.isPending} onClick={onCancel}
          className="rounded px-3 py-2 text-sm text-gray-400 hover:bg-gray-800">Cancel</button>
      </div>
    </form>
  );
}
