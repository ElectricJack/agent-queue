import { useId } from "react";
import { useProfiles } from "../../api/hooks";
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
  value, onChange,
}: {
  value: DefinitionForm;
  onChange: (next: DefinitionForm) => void;
}) {
  const id = useId();
  const { data: profiles = [] } = useProfiles();
  const { data: classes } = useAgentIntelligenceClasses();
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
          {value.profile_id && !profiles.some((p) => p.id === value.profile_id) && (
            <option value={value.profile_id}>{value.profile_id}</option>
          )}
          {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name || profile.id}</option>)}
        </select>
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
