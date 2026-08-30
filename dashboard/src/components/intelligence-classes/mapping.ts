export function providerFields(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function describeProviderMapping(value: unknown): string {
  if (value === null) return "No override (null)";
  const slice = providerFields(value);
  if (!slice) return "Custom value: " + JSON.stringify(value);
  const bits: string[] = [];
  if (typeof slice.model === "string" && slice.model) bits.push(slice.model);
  if (typeof slice.thinking === "string" && slice.thinking) bits.push("think:" + slice.thinking);
  if (typeof slice.reasoning_effort === "string" && slice.reasoning_effort) bits.push("effort:" + slice.reasoning_effort);
  if (typeof slice.thinking_budget === "number") bits.push("budget:" + slice.thinking_budget);
  return bits.join(" · ") || (Object.keys(slice).length ? "Custom options" : "No override (empty)");
}

export function groupIntelligenceClasses<T extends { id: string }>(rows: T[]) {
  const tiers = ["fast", "standard", "deep"];
  const levels = ["off", "low", "medium", "high"];
  const builtin = (id: string) => /^(fast|standard|deep)-(off|low|medium|high)$/.test(id);
  const groups = tiers.map((tier) => ({
    label: tier + " tier",
    rows: rows.filter((row) => builtin(row.id) && row.id.startsWith(tier + "-"))
      .sort((a, b) => levels.indexOf(a.id.split("-")[1]!) - levels.indexOf(b.id.split("-")[1]!)),
  }));
  groups.push({ label: "Custom classes", rows: rows.filter((row) => !builtin(row.id)).sort((a, b) => a.id.localeCompare(b.id)) });
  return groups.filter((group) => group.rows.length > 0);
}
