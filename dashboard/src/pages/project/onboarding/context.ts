import { createContext, useContext, useEffect, useId, useRef, type Dispatch, type RefObject } from "react";
import type { ProjectRootsSource } from "./useProjectRoots";
import type { StepId, WizardAction, WizardState } from "./state";

export interface WizardContextValue {
  state: WizardState;
  dispatch: Dispatch<WizardAction>;
  roots: ProjectRootsSource;
  /** The step whose panel is currently rendered. */
  stepId: StepId;
  /** Remember which step owns a field so the error summary can jump to it. */
  registerField: (name: string, stepId: StepId) => void;
  /** Field the error summary asked to focus once its step renders. */
  pendingFocusField: string | null;
  clearPendingFocus: () => void;
}

export const WizardContext = createContext<WizardContextValue | null>(null);

/** Access the wizard store from a step panel. */
export function useWizard(): WizardContextValue {
  const ctx = useContext(WizardContext);
  if (!ctx) throw new Error("useWizard must be used inside ProjectOnboardingWizard");
  return ctx;
}

/** Operator-facing labels for the fields the onboarding request carries. */
export const FIELD_LABELS: Record<string, string> = {
  projectName: "Project name",
  projectId: "Project ID",
  defaultBranch: "Default branch",
  rootId: "Project root",
  relativePath: "Repository",
  directoryName: "Directory name",
  githubUrl: "GitHub repository",
  githubRepository: "GitHub repository",
  githubOwner: "GitHub owner",
  githubRepo: "GitHub repository name",
  githubVisibility: "Visibility",
};

export function fieldLabel(name: string): string {
  const known = FIELD_LABELS[name];
  if (known) return known;
  const words = name.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function fieldErrorId(name: string): string {
  return `onboarding-field-error-${name}`;
}

export function currentFieldError(state: WizardState, name: string): string | null {
  if (state.submission.status !== "failed") return null;
  return state.submission.error.fieldErrors[name] ?? null;
}

export interface FieldErrorProps<E extends HTMLElement> {
  ref: RefObject<E | null>;
  "aria-invalid": true | undefined;
  "aria-describedby": string | undefined;
}

/**
 * Props to spread on an input so a server-side field error is associated
 * with it (`aria-describedby` → the matching `<FieldError>`), and so the
 * error summary's "jump to field" action can focus it.
 */
export function useFieldErrorProps<E extends HTMLElement = HTMLInputElement>(name: string): FieldErrorProps<E> {
  const { state, stepId, registerField, pendingFocusField, clearPendingFocus } = useWizard();
  const ref = useRef<E | null>(null);
  const error = currentFieldError(state, name);
  useEffect(() => registerField(name, stepId), [name, stepId, registerField]);
  useEffect(() => {
    if (pendingFocusField !== name) return;
    ref.current?.focus();
    clearPendingFocus();
  }, [pendingFocusField, name, clearPendingFocus]);
  return {
    ref,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? fieldErrorId(name) : undefined,
  };
}

/** Stable id helper for step panels that need to label their own controls. */
export function useStepIds(prefix: string) {
  const id = useId();
  return (suffix: string) => `${prefix}-${id}-${suffix}`;
}
