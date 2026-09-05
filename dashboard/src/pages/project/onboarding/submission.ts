import { getProjectOnboarding, onboardProject } from "../../../api/client";
import type { SubmitProject, WizardSubmission } from "./ProjectOnboardingWizard";
import type { OnboardingResult, SubmissionError } from "./state";

/** Normalize whatever a `SubmitProject` rejected with into a `SubmissionError`. */
export function toSubmissionError(err: unknown): SubmissionError {
  if (typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string") {
    const e = err as { message: string; code?: unknown; phase?: unknown; details?: unknown; fieldErrors?: unknown };
    const fieldErrors: Record<string, string> = {};
    if (typeof e.fieldErrors === "object" && e.fieldErrors !== null) {
      for (const [k, v] of Object.entries(e.fieldErrors as Record<string, unknown>)) {
        if (typeof v === "string") fieldErrors[k] = v;
      }
    }
    return { message: e.message, code: typeof e.code === "string" ? e.code : undefined, phase: typeof e.phase === "string" ? e.phase : undefined, survivors: survivors(e), fieldErrors };
  }
  return { message: "Project creation failed.", fieldErrors: {} };
}

export function newRequestId(): string {
  return crypto.randomUUID();
}

/** Build the one daemon-owned request, then poll its durable phase while it is in flight. */
export function daemonSubmit(requestId: string): SubmitProject {
  return async (request, { onPhase }) => {
    const timer = window.setInterval(() => {
      void getProjectOnboarding({ body: { request_id: requestId }, throwOnError: true }).then(({ data }) => {
        if (data.phase) onPhase(data.phase);
      }).catch(() => undefined);
    }, 750);
    try {
      const { data } = await onboardProject({ body: requestBody(requestId, request), throwOnError: true });
      return data as OnboardingResult;
    } catch (error) {
      throw toSubmissionError(error);
    } finally {
      window.clearInterval(timer);
    }
  };
}

function requestBody(requestId: string, request: WizardSubmission) {
  const { source, identity } = request;
  if (source.mode === null) throw new Error("A source mode is required before submission");
  const common = {
    request_id: requestId,
    source_mode: source.mode,
    root_id: source.rootId ?? "",
    project_name: identity.projectName,
    project_id: identity.projectId,
    default_branch: identity.defaultBranch,
  };
  if (source.mode === "link") return { ...common, relative_path: source.relativePath ?? "" };
  if (source.mode === "init") return {
    ...common, relative_path: source.directoryName, create_readme: source.createReadme, create_github: source.createGithub,
    ...(source.createGithub ? { github_owner: source.githubOwner, github_repo: source.githubRepo || source.directoryName, github_visibility: source.githubVisibility } : {}),
  };
  if (source.mode === "github_clone") {
    const urlParts = source.githubUrl.split("/").filter(Boolean);
    const destination = urlParts[urlParts.length - 1]?.replace(/\.git$/, "") ?? "";
    return {
      ...common, relative_path: source.githubRepository?.name ?? destination,
      ...(source.githubRepository ? { github_repository: { owner: source.githubRepository.owner, name: source.githubRepository.name } } : { github_url: source.githubUrl }),
    };
  }
  throw new Error("Unsupported source mode");
}

function survivors(error: { details?: unknown }): string[] | undefined {
  if (!error.details || typeof error.details !== "object") return undefined;
  const details = error.details as Record<string, unknown>;
  return [details.canonical_path, details.github_repository_url, details.github_url].filter((value): value is string => typeof value === "string");
}
