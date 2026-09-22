export const PLAYBOOK_POSITION_SCOPE = "__playbooks__";

export interface ManualPosition {
  x: number;
  y: number;
}

/** Pins survive only for the separate playbook graph, never project tasks. */
export type ManualPositions = Partial<Record<typeof PLAYBOOK_POSITION_SCOPE, Record<string, ManualPosition>>>;
