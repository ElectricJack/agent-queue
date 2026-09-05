import type { ComponentType } from "react";
import { STEP_IDS, type StepId } from "./state";
import { STEP_TITLES } from "./copy";
import { ChooseSourceStep } from "./ChooseSourceStep";
import { InitOptionsStep } from "./InitOptionsStep";
import { ChooseRepositoryStep } from "./ChooseRepositoryStep";
import { ProjectIdentityStep } from "./ProjectIdentityStep";
import { ReviewProjectStep } from "./ReviewProjectStep";

export interface WizardStep {
  id: StepId;
  title: string;
  Component: ComponentType;
}

export const DEFAULT_STEP_COMPONENTS: Record<StepId, ComponentType> = {
  source: ChooseSourceStep,
  repository: ChooseRepositoryStep,
  identity: ProjectIdentityStep,
  options: InitOptionsStep,
  review: ReviewProjectStep,
};

/**
 * Build the ordered step registry, replacing any panel with a plugged-in
 * component. Sibling tasks call this so they never touch the shell.
 */
export function createStepRegistry(overrides: Partial<Record<StepId, ComponentType>> = {}): WizardStep[] {
  return STEP_IDS.map((id) => ({
    id,
    title: STEP_TITLES[id],
    Component: overrides[id] ?? DEFAULT_STEP_COMPONENTS[id],
  }));
}

export const defaultStepRegistry: WizardStep[] = createStepRegistry();
