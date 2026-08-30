import { providerFields } from "../../components/intelligence-classes/mapping";

export const PROVIDERS: Record<string, { label: string; effort?: { key: string; label: string; values: string[] } }> = {
  anthropic: { label: "Claude", effort: { key: "thinking", label: "thinking", values: ["off", "low", "medium", "high", "xhigh", "max"] } },
  openai: { label: "OpenAI", effort: { key: "reasoning_effort", label: "reasoning effort", values: ["none", "minimal", "low", "medium", "high", "xhigh"] } },
  codex: { label: "Codex", effort: { key: "reasoning_effort", label: "reasoning effort", values: ["none", "minimal", "low", "medium", "high", "xhigh"] } },
  google: { label: "Google" },
};

function isFableModel(value: unknown): boolean {
  return typeof value === "string" && /^claude-fable(?:-|$)/i.test(value.trim());
}

export function providerEffort(provider: string, model: unknown) {
  const effort = PROVIDERS[provider]?.effort;
  return effort && provider === "anthropic" && isFableModel(model)
    ? { ...effort, values: effort.values.filter((value) => value !== "off") }
    : effort;
}

function finiteJson(value: unknown): boolean {
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(finiteJson);
  const fields = providerFields(value);
  return !fields || Object.values(fields).every(finiteJson);
}

export function parseMapping(text: string): { mapping: Record<string, unknown>; error?: never } | { mapping?: never; error: string } {
  try {
    const value: unknown = JSON.parse(text);
    const mapping = providerFields(value);
    if (!mapping) return { error: "Provider mapping must be a JSON object." };
    if (!finiteJson(mapping)) return { error: "Provider mapping must contain finite numbers." };
    if (new TextEncoder().encode(JSON.stringify(mapping)).byteLength > 65_536) {
      return { error: "Provider mapping must be no larger than 64 KiB." };
    }
    return { mapping };
  } catch {
    return { error: "Provider mapping must be valid JSON." };
  }
}

export function validateMappingChanges(mapping: Record<string, unknown>, original: Record<string, unknown>): string | null {
  for (const [provider, { label, effort }] of Object.entries(PROVIDERS)) {
    const slice = providerFields(mapping[provider]);
    if (!slice) continue;
    const previous = providerFields(original[provider]) ?? {};
    const changed = (key: string) => Object.prototype.hasOwnProperty.call(slice, key)
      && JSON.stringify(slice[key]) !== JSON.stringify(previous[key]);
    if (changed("model") && (typeof slice.model !== "string" || !slice.model.trim() || slice.model !== slice.model.trim()
      || slice.model.length > 200 || [...slice.model].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127))) {
      return label + " model must be a nonempty name of up to 200 characters, without surrounding whitespace or control characters.";
    }
    if (provider === "anthropic" && isFableModel(slice.model) && slice.thinking === "off"
      && (changed("model") || changed("thinking"))) {
      return "Fable does not support thinking off. Choose low or higher.";
    }
    if (effort && changed(effort.key) && (typeof slice[effort.key] !== "string" || !effort.values.includes(slice[effort.key] as string))) {
      return label + " " + effort.label + " must be one of: " + effort.values.join(", ") + ".";
    }
    if (provider === "google" && changed("thinking_budget")
      && (typeof slice.thinking_budget !== "number" || !Number.isSafeInteger(slice.thinking_budget) || slice.thinking_budget < 0)) {
      return "Google thinking budget must be a whole number of zero or more.";
    }
  }
  return null;
}

export function fieldText(value: unknown): string {
  if (value == null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}
