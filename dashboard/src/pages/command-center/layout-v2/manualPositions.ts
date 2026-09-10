export const PLAYBOOK_POSITION_SCOPE = "__playbooks__";

export interface ManualPosition {
  x: number;
  y: number;
}

export type ManualPositions = Record<string, Record<string, ManualPosition>>;
