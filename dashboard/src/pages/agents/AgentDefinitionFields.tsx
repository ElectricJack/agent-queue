import { useId } from "react";
import { usePoolStatus, useProfiles } from "../../api/hooks";
import { poolProfileIds } from "./pools";
import { useAgentIntelligenceClasses } from "../../api/agents";

export interface DefinitionForm {
  name: string;
  profile_id: string;
  harness: string;
  model: string;
  intelligence_class: string;
  enabled: boolean;
}

const inputClass = "mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";
const harnesses = [
  { id: "claude", label: "Anthropic / Claude" },
  { id: "codex", label: "OpenAI / Codex" },
  { id: "gemini", label: "Google / Gemini" },
];

export default function AgentDefinitionFields({
  value, onChange, allowSupervisor = false, allowPoolProfiles = true,
}: {
  value: DefinitionForm;
  allowSupervisor?: boolean;
  /**
   * Offer ``lifecycle: pool`` profiles in the profile picker. False on the
   * create-an-agent form, where such a profile would silently produce pool
   * capacity instead of the durable worker the operator asked for.
   */
  allowPoolProfiles?: boolean;
  onChange: (next: DefinitionForm) => void;
}) {
  const id = useId();
  const { data: profiles = [] } = useProfiles();
  const { data: classes } = useAgentIntelligenceClasses();
  const poolIds = poolProfileIds(usePoolStatus().data ?? [], profiles);
  const lifecycle = value.profile_id ? (poolIds.has(value.profile_id) ? "pool" : "task") : null;
  const availableProfiles = profiles.filter((profile) => allowSupervisor || profile.id !== "supervisor");
  // A pool profile stays visible but unselectable when pools are disallowed:
  // hiding it entirely leaves an operator hunting for a profile they know
  // exists, with nothing to say why it is not on offer here.
  const ineligible = (profile: { id: string }) =>
    !allowPoolProfiles && poolIds.has(profile.id);
  const set = <K extends keyof DefinitionForm>(key: K, next: DefinitionForm[K]) => onChange({ ...value, [key]: next });
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <label className="text-xs text-gray-400" htmlFor={id + "-name"}>
        Name
        <input id={id + "-name"} required value={value.name} onChange={(e) => set("name", e.target.value)} className={inputClass} />
      </label>
      <label className="text-xs text-gray-400" htmlFor={id + "-profile"}>
        Profile
        <select id={id + "-profile"} required value={value.profile_id} onChange={(e) => set("profile_id", e.target.value)} className={inputClass}>
          <option value="">Choose a profile</option>
          {value.profile_id && (allowSupervisor || value.profile_id !== "supervisor") && !availableProfiles.some((p) => p.id === value.profile_id) && (
            <option value={value.profile_id}>{value.profile_id}</option>
          )}
          {availableProfiles.map((profile) => (
            <option key={profile.id} value={profile.id} disabled={ineligible(profile)}>
              {(profile.name || profile.id) + (ineligible(profile) ? " — pool profile" : "")}
            </option>
          ))}
        </select>
        {lifecycle && (
          <span className="mt-1 block text-[10px] text-gray-500">
            Lifecycle: <span className={lifecycle === "pool" ? "text-sky-300" : "text-gray-300"}>{lifecycle}</span>
            {lifecycle === "pool"
              ? " — the daemon sizes this profile as a worker pool and its sessions claim their own tasks."
              : " — sessions are started per assigned task."}
          </span>
        )}
      </label>
      <label className="text-xs text-gray-400" htmlFor={id + "-harness"}>
        Provider / harness
        <select id={id + "-harness"} value={value.harness} onChange={(e) => set("harness", e.target.value)} className={inputClass}>
          <option value="">Inherit from profile</option>
          {value.harness && !harnesses.some((h) => h.id === value.harness) && (
            <option value={value.harness}>{value.harness}</option>
          )}
          {harnesses.map((harness) => <option key={harness.id} value={harness.id}>{harness.label}</option>)}
        </select>
      </label>
      <label className="text-xs text-gray-400" htmlFor={id + "-model"}>
        Model override
        <input id={id + "-model"} value={value.model} onChange={(e) => set("model", e.target.value)} placeholder="Inherit from profile / intelligence level" className={inputClass} />
      </label>
      <div>
        <label className="text-xs text-gray-400" htmlFor={id + "-intelligence"}>Intelligence level</label>
        <input id={id + "-intelligence"} list={id + "-classes"} value={value.intelligence_class}
          onChange={(e) => set("intelligence_class", e.target.value)} placeholder="Inherit from profile" className={inputClass} />
        <datalist id={id + "-classes"}>
          {(classes?.classes ?? []).map((level) => <option key={level.id} value={level.id}>{level.name}</option>)}
        </datalist>
      </div>
      <label className="flex items-center gap-2 self-center text-sm text-gray-300">
        <input type="checkbox" checked={value.enabled} onChange={(e) => set("enabled", e.target.checked)} className="accent-indigo-500" />
        Enabled for new work
      </label>
      <p className="text-xs text-gray-500 sm:col-span-2">
        Blank overrides inherit from the profile. Provider follows the selected harness.
      </p>
    </div>
  );
}
