/**
 * State store for the project onboarding wizard (design §4.2).
 *
 * A plain reducer so the sibling step tasks (directory browser, GitHub
 * step, identity/review) can plug in through `useWizard()` without touching
 * the shell. The rules encoded here:
 *
 * - back navigation preserves every entered value;
 * - switching source mode keeps the shared identity values and resets the
 *   source-specific ones to their defaults;
 * - navigation is frozen while a submission is in flight;
 * - a failed submission keeps every (non-secret) form value.
 */

export const STEP_IDS = ["source", "repository", "identity", "options", "review"] as const;
export type StepId = (typeof STEP_IDS)[number];

export const SOURCE_MODES = ["link", "init", "github_clone"] as const;
export type SourceMode = (typeof SOURCE_MODES)[number];

export interface IdentityValues {
  projectName: string;
  projectId: string;
  defaultBranch: string;
}

export interface LinkSource {
  mode: "link";
  rootId: string | null;
  /** Root-relative path of the selected Git repository (design §3.3). */
  relativePath: string | null;
}

export type GithubVisibility = "private" | "public";

export interface InitSource {
  mode: "init";
  rootId: string | null;
  directoryName: string;
  createReadme: boolean;
  createGithub: boolean;
  githubOwner: string | null;
  githubRepo: string;
  githubVisibility: GithubVisibility;
}

export interface GithubRepositoryRef {
  owner: string;
  name: string;
  cloneUrl: string;
  defaultBranch: string | null;
  visibility?: GithubVisibility;
}

export interface GithubCloneSource {
  mode: "github_clone";
  rootId: string | null;
  githubRepository: GithubRepositoryRef | null;
  /** Unmodified pasted input; the daemon, not the browser, validates it. */
  githubUrl: string;
  /** Root-relative clone destination, initially inferred from the repository. */
  directoryName: string;
  /** Keeps the suggested destination in sync until the operator edits it. */
  directoryNameAuto: boolean;
}

export interface NoSource {
  mode: null;
}

export type SourceState = NoSource | LinkSource | InitSource | GithubCloneSource;
export type SourceFor<M extends SourceMode> = Extract<SourceState, { mode: M }>;

export interface SubmissionError {
  message: string;
  code?: string;
  phase?: string;
  survivors?: string[];
  /** Field errors keyed by the identity/source field they belong to. */
  fieldErrors: Record<string, string>;
}

export interface OnboardingResult {
  project_id: string;
  [key: string]: unknown;
}

export type SubmissionState =
  | { status: "idle" }
  | { status: "submitting"; phase: string | null }
  | { status: "failed"; error: SubmissionError }
  | { status: "succeeded"; result: OnboardingResult };

export interface WizardState {
  stepIndex: number;
  /** Highest step index the operator has reached; `go_to` may not pass it. */
  furthestStepIndex: number;
  source: SourceState;
  identity: IdentityValues;
  projectIds: string[];
  submission: SubmissionState;
}

export type WizardAction =
  | { type: "set_source_mode"; mode: SourceMode }
  | { [M in SourceMode]: { type: "update_source"; mode: M; patch: Partial<Omit<SourceFor<M>, "mode">> } }[SourceMode]
  | { type: "update_identity"; patch: Partial<IdentityValues> }
  | { type: "go_next" }
  | { type: "go_back" }
  | { type: "go_to"; step: StepId }
  | { type: "submit_started" }
  | { type: "submit_phase"; phase: string }
  | { type: "submit_failed"; error: SubmissionError }
  | { type: "submit_succeeded"; result: OnboardingResult }
  | { type: "reset" };

export const DEFAULT_BRANCH = "main";

export function defaultSource(mode: SourceMode): SourceState {
  switch (mode) {
    case "link":
      return { mode, rootId: null, relativePath: null };
    case "init":
      return {
        mode,
        rootId: null,
        directoryName: "",
        createReadme: true,
        createGithub: false,
        githubOwner: null,
        githubRepo: "",
        githubVisibility: "private",
      };
    case "github_clone":
      return { mode, rootId: null, githubRepository: null, githubUrl: "", directoryName: "", directoryNameAuto: true };
  }
}

export function initialWizardState(projectIds: readonly string[] = []): WizardState {
  return {
    stepIndex: 0,
    furthestStepIndex: 0,
    source: { mode: null },
    identity: { projectName: "", projectId: "", defaultBranch: DEFAULT_BRANCH },
    projectIds: [...projectIds],
    submission: { status: "idle" },
  };
}

export function stepIndexOf(step: StepId): number {
  return STEP_IDS.indexOf(step);
}

/** Whether the shell's Next button may leave the current step. */
export function canAdvance(state: WizardState): boolean {
  if (state.submission.status === "submitting") return false;
  if (state.stepIndex >= STEP_IDS.length - 1) return false;
  if (STEP_IDS[state.stepIndex] === "source") return state.source.mode !== null;
  return true;
}

export function reviewActionLabel(mode: SourceMode | null): string {
  switch (mode) {
    case "link":
      return "Link project";
    case "github_clone":
      return "Clone and add project";
    case "init":
    case null:
      return "Create project";
  }
}

function clampStep(index: number): number {
  return Math.max(0, Math.min(STEP_IDS.length - 1, index));
}

function moveTo(state: WizardState, index: number): WizardState {
  const stepIndex = clampStep(index);
  if (stepIndex === state.stepIndex) return state;
  return {
    ...state,
    stepIndex,
    furthestStepIndex: Math.max(state.furthestStepIndex, stepIndex),
  };
}

export function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  const submitting = state.submission.status === "submitting";
  switch (action.type) {
    case "set_source_mode": {
      if (submitting || state.source.mode === action.mode) return state;
      return { ...state, source: defaultSource(action.mode) };
    }
    case "update_source": {
      if (submitting || state.source.mode !== action.mode) return state;
      return { ...state, source: { ...state.source, ...action.patch } as SourceState };
    }
    case "update_identity": {
      if (submitting) return state;
      return { ...state, identity: { ...state.identity, ...action.patch } };
    }
    case "go_next":
      return canAdvance(state) ? moveTo(state, state.stepIndex + 1) : state;
    case "go_back":
      return submitting ? state : moveTo(state, state.stepIndex - 1);
    case "go_to": {
      if (submitting) return state;
      const target = stepIndexOf(action.step);
      if (target < 0 || target > state.furthestStepIndex) return state;
      return moveTo(state, target);
    }
    case "submit_started":
      return { ...state, submission: { status: "submitting", phase: null } };
    case "submit_phase":
      return submitting ? { ...state, submission: { status: "submitting", phase: action.phase } } : state;
    case "submit_failed":
      return { ...state, submission: { status: "failed", error: action.error } };
    case "submit_succeeded":
      return { ...state, submission: { status: "succeeded", result: action.result } };
    case "reset":
      return initialWizardState();
  }
}
