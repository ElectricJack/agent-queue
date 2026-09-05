import { getProjectOnboarding, onboardProject } from "../../../api/client";
import type { SubmitProject, WizardSubmission } from "./ProjectOnboardingWizard";
import type { OnboardingResult, SubmissionError } from "./state";

/** Normalize whatever a `SubmitProject` rejected with into a `SubmissionError`. */
export function toSubmissionError(err: unknown): SubmissionError {
  const e = errorPayload(err);
  if (e) return { message: e.error ?? e.message ?? "Project creation failed.", code: e.error_code ?? e.code, phase: e.phase, survivors: survivors(e), fieldErrors: fieldErrors(e) };
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

export function requestBody(requestId: string, request: WizardSubmission) {
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
    return {
      ...common, relative_path: source.directoryName,
      ...(source.githubRepository ? { github_repository: { owner: source.githubRepository.owner, name: source.githubRepository.name } } : { github_url: source.githubUrl }),
    };
  }
  throw new Error("Unsupported source mode");
}

type ErrorPayload = {
  message?: string;
  error?: string;
  code?: string;
  error_code?: string;
  phase?: string;
  details?: unknown;
  fieldErrors?: unknown;
  field_errors?: unknown;
  payload?: unknown;
};

function errorPayload(error: unknown): ErrorPayload | null {
  if (typeof error !== "object" || error === null) return null;
  const value = error as ErrorPayload;
  if (typeof value.payload === "object" && value.payload !== null) return value.payload as ErrorPayload;
  if (typeof value.message === "string" && value.message.startsWith("API ")) {
    const json = value.message.slice(value.message.indexOf(":") + 1);
    try {
      const parsed = JSON.parse(json);
      return typeof parsed === "object" && parsed !== null ? parsed as ErrorPayload : value;
    } catch {
      return value;
    }
  }
  return value;
}

function fieldErrors(error: ErrorPayload): Record<string, string> {
  const values = error.fieldErrors ?? error.field_errors;
  if (!values || typeof values !== "object") return {};
  if (Array.isArray(values)) {
    return Object.fromEntries(values.flatMap((value) => {
      if (typeof value !== "object" || value === null) return [];
      const item = value as { field?: unknown; message?: unknown };
      return typeof item.field === "string" && typeof item.message === "string" ? [[item.field, item.message]] : [];
    }));
  }
  return Object.fromEntries(Object.entries(values).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
}
function survivors(error: { details?: unknown }): string[] | undefined {
  if (!error.details || typeof error.details !== "object") return undefined;
  const details = error.details as Record<string, unknown>;
  return [details.canonical_path, details.github_repository_url, details.github_url].filter((value): value is string => typeof value === "string");
}
