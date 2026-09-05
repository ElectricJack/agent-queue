import { useCallback, useEffect, useId, useMemo, useReducer, useRef, useState, type RefObject } from "react";
import { Link } from "react-router-dom";
import { CheckIcon, XMarkIcon } from "@heroicons/react/24/outline";
import {
  canAdvance,
  initialWizardState,
  reviewActionLabel,
  wizardReducer,
  type IdentityValues,
  type OnboardingResult,
  type SourceMode,
  type SourceState,
  type StepId,
} from "./state";
import { WizardContext, fieldLabel, type WizardContextValue } from "./context";
import { defaultStepRegistry, type WizardStep } from "./stepRegistry";
import { daemonSubmit, newRequestId, toSubmissionError } from "./submission";
import { useFocusTrap } from "./useFocusTrap";
import type { ProjectRootsSource } from "./useProjectRoots";

/** Where the Settings UI manages configured project roots (design §3.2). */
export const PROJECT_ROOTS_SETTINGS_PATH = "/settings/config";

export interface WizardSubmission {
  mode: SourceMode;
  source: SourceState;
  identity: IdentityValues;
}

export interface SubmitContext {
  /** Report a long-running phase ("Cloning repository", …) for the live region. */
  onPhase: (phase: string) => void;
}

/**
 * Performs the onboarding request. Reject with a `SubmissionError`-shaped
 * value (`message`, optional `code`, `fieldErrors`) to surface field errors.
 */
export type SubmitProject = (request: WizardSubmission, ctx: SubmitContext) => Promise<OnboardingResult>;

export interface ProjectOnboardingWizardProps {
  open: boolean;
  onClose: () => void;
  /** Element that opened the wizard; focus returns to it on close. */
  returnFocusRef?: RefObject<HTMLElement | null>;
  roots: ProjectRootsSource;
  /** IDs from the already-loaded left-rail project list for immediate collision feedback. */
  projectIds?: readonly string[];
  steps?: WizardStep[];
  submit?: SubmitProject;
  onSuccess?: (result: OnboardingResult) => void;
}

export default function ProjectOnboardingWizard(props: ProjectOnboardingWizardProps) {
  if (!props.open) return null;
  return <WizardDialog {...props} />;
}

const primaryButton =
  "rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-300 disabled:opacity-50";
const secondaryButton =
  "rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-300 disabled:opacity-50";

function WizardDialog({
  onClose,
  returnFocusRef,
  roots,
  projectIds = [],
  steps = defaultStepRegistry,
  submit,
  onSuccess,
}: ProjectOnboardingWizardProps) {
  const [state, dispatch] = useReducer(wizardReducer, projectIds, initialWizardState);
  const requestId = useRef(newRequestId()).current;
  const dialogRef = useRef<HTMLDivElement>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const fieldSteps = useRef(new Map<string, StepId>());
  const [pendingFocusField, setPendingFocusField] = useState<string | null>(null);
  const uid = useId();
  const titleId = `${uid}-title`;
  const stepsId = `${uid}-steps`;

  const step = steps[state.stepIndex] ?? steps[0]!;
  const stepId = step.id;
  const submitting = state.submission.status === "submitting";
  const failed = state.submission.status === "failed" ? state.submission.error : null;
  const effectiveSubmit = useMemo(() => submit ?? daemonSubmit(requestId), [submit, requestId]);
  const noRoots = roots.status === "ready" && roots.roots.length === 0;
  const isReview = stepId === "review";

  useFocusTrap(dialogRef, true);

  // Restore focus to the opener when the dialog unmounts (design §9).
  useEffect(() => {
    const opener = returnFocusRef?.current;
    return () => opener?.focus();
  }, [returnFocusRef]);

  const close = useCallback(() => {
    if (submitting) return;
    onClose();
  }, [onClose, submitting]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    };
    document.addEventListener("keydown", handler, true);
    return () => document.removeEventListener("keydown", handler, true);
  }, [close]);

  useEffect(() => {
    if (failed) summaryRef.current?.focus();
  }, [failed]);

  const registerField = useCallback((name: string, owner: StepId) => {
    fieldSteps.current.set(name, owner);
  }, []);
  const clearPendingFocus = useCallback(() => setPendingFocusField(null), []);

  const ctx = useMemo<WizardContextValue>(
    () => ({ state, dispatch, roots, projectIds: state.projectIds, stepId, registerField, pendingFocusField, clearPendingFocus }),
    [state, roots, stepId, registerField, pendingFocusField, clearPendingFocus],
  );

  const announcement = (() => {
    if (roots.status === "loading") return "Loading project roots";
    if (roots.status === "error") return `Project roots unavailable: ${roots.message}`;
    if (noRoots) return "No project roots configured";
    switch (state.submission.status) {
      case "submitting":
        return `Creating project: ${state.submission.phase ?? "starting"}`;
      case "failed":
        return `Project creation failed: ${state.submission.error.message}`;
      case "succeeded":
        return "Project created";
      case "idle":
        return `Step ${state.stepIndex + 1} of ${steps.length}: ${step.title}`;
    }
  })();

  const runSubmit = async () => {
    if (state.source.mode === null || submitting) return;
    const request: WizardSubmission = { mode: state.source.mode, source: state.source, identity: state.identity };
    dispatch({ type: "submit_started" });
    try {
      const result = await effectiveSubmit(request, { onPhase: (phase) => dispatch({ type: "submit_phase", phase }) });
      dispatch({ type: "submit_succeeded", result });
      onSuccess?.(result);
      onClose();
    } catch (err) {
      dispatch({ type: "submit_failed", error: toSubmissionError(err) });
    }
  };

  const jumpToField = (name: string) => {
    const owner = fieldSteps.current.get(name);
    if (!owner) return;
    dispatch({ type: "go_to", step: owner });
    setPendingFocusField(name);
  };

  const StepComponent = step.Component;

  return (
    <WizardContext.Provider value={ctx}>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={close}>
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          onClick={(e) => e.stopPropagation()}
          className="mx-3 flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-gray-700 bg-gray-900 shadow-xl outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          <div className="flex items-center justify-between border-b border-gray-700 px-5 py-3">
            <h2 id={titleId} className="text-lg font-semibold">Add project</h2>
            <button
              type="button"
              aria-label="Close dialog"
              onClick={close}
              disabled={submitting}
              className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300 disabled:opacity-50"
            >
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>

          <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
            {announcement}
          </div>

          <div className="flex-1 overflow-y-auto p-5">
            {roots.status === "loading" && <p className="text-sm text-gray-400">Loading project roots…</p>}
            {roots.status === "error" && (
              <p className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
                <span aria-hidden="true">⚠ </span>Project roots are unavailable: {roots.message}
              </p>
            )}
            {noRoots && <NoRootsEmptyState onNavigate={onClose} />}
            {roots.status === "ready" && !noRoots && (
              <div className="space-y-5">
                <ol id={stepsId} aria-label="Steps" className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                  {steps.map((s, i) => {
                    const status = i < state.stepIndex ? "completed" : i === state.stepIndex ? "current" : "upcoming";
                    return (
                      <li
                        key={s.id}
                        aria-current={status === "current" ? "step" : undefined}
                        className={`flex items-center gap-1 ${
                          status === "current" ? "font-semibold text-indigo-200" : status === "completed" ? "text-gray-300" : "text-gray-500"
                        }`}
                      >
                        <span aria-hidden="true" className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-current">
                          {status === "completed" ? <CheckIcon className="h-3 w-3" /> : i + 1}
                        </span>
                        <span>{s.title}</span>
                        <span className="sr-only">
                          {status === "completed" ? "(completed)" : status === "current" ? "(current step)" : ""}
                        </span>
                      </li>
                    );
                  })}
                </ol>

                {failed && (
                  <div
                    ref={summaryRef}
                    role="alert"
                    tabIndex={-1}
                    aria-labelledby={`${uid}-error-title`}
                    className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200 outline-none focus-visible:ring-2 focus-visible:ring-red-300"
                  >
                    <p id={`${uid}-error-title`} className="font-medium">
                      <span aria-hidden="true">⚠ </span>
                      {failed.message}
                      {failed.code && <span className="ml-2 font-mono text-xs text-red-300/80">({failed.code})</span>}
                    </p>
                    {Object.keys(failed.fieldErrors).length > 0 && (
                      <ul className="mt-2 list-disc space-y-1 pl-5">
                        {Object.entries(failed.fieldErrors).map(([name, message]) =>
                          fieldSteps.current.has(name) ? (
                            <li key={name}>
                              <button type="button" onClick={() => jumpToField(name)} className="underline hover:text-white">
                                {fieldLabel(name)}: {message}
                              </button>
                            </li>
                          ) : (
                            <li key={name}>{fieldLabel(name)}: {message}</li>
                          ),
                        )}
                      </ul>
                    )}
                    {failed.phase && <p className="mt-2">Failed during: {failed.phase}</p>}
                    {failed.survivors?.map((survivor) => <p key={survivor} className="mt-1">Still exists: {survivor}</p>)}
                  </div>
                )}

                <section aria-labelledby={`${uid}-step-title`}>
                  <h3 id={`${uid}-step-title`} className="mb-3 text-sm font-medium text-gray-200">{step.title}</h3>
                  <StepComponent />
                </section>

                {submitting && (
                  <p className="flex items-center gap-2 text-sm text-gray-300">
                    <span aria-hidden="true" className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-indigo-300 border-t-transparent" />
                    <span>Creating project — {state.submission.status === "submitting" && state.submission.phase ? state.submission.phase : "starting"}…</span>
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-gray-800 px-5 py-3">
            <button type="button" onClick={close} disabled={submitting} className={secondaryButton}>
              Cancel
            </button>
            {roots.status === "ready" && !noRoots && (
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => dispatch({ type: "go_back" })}
                  disabled={state.stepIndex === 0 || submitting}
                  className={secondaryButton}
                >
                  Back
                </button>
                {isReview ? (
                  <button
                    type="button"
                    onClick={() => void runSubmit()}
                    disabled={submitting || state.source.mode === null}
                    className={primaryButton}
                  >
                    {reviewActionLabel(state.source.mode)}
                  </button>
                ) : (
                  <button type="button" onClick={() => dispatch({ type: "go_next" })} disabled={!canAdvance(state)} className={primaryButton}>
                    Next
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </WizardContext.Provider>
  );
}

function NoRootsEmptyState({ onNavigate }: { onNavigate: () => void }) {
  return (
    <div className="space-y-3 rounded-lg border border-dashed border-gray-700 p-5 text-center">
      <p className="text-sm font-medium text-gray-200">No project roots configured</p>
      <p className="text-sm text-gray-400">
        Projects can only be added beneath a configured project root on the daemon host. Add one under
        Settings, then come back here.
      </p>
      <Link
        to={PROJECT_ROOTS_SETTINGS_PATH}
        onClick={onNavigate}
        className="inline-block rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-300"
      >
        Open Settings to add a project root
      </Link>
    </div>
  );
}
