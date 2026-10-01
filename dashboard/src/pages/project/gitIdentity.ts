// Project Git commit identity: form fields, client-side validation and the
// edit_project payload, shared by the Project Settings page (Config.tsx) and
// its contextual-settings twin (ProjectSubject.tsx).
//
// Resolution (src/git/identity.py): the project's override pair, else the
// installation default (`git_identity:` in config.yaml), else the documented
// fallback. The validation below mirrors validate_git_name / validate_git_email
// there; the daemon re-validates every save.
import type { EffectiveGitIdentity } from "../../api/client";

export type GitIdentitySource = EffectiveGitIdentity["source"];

export interface GitIdentityFormFields {
  /** True when the project keeps its own pair instead of inheriting. */
  git_identity_override: boolean;
  git_identity_name: string;
  git_identity_email: string;
}

export interface GitIdentityFieldErrors {
  name?: string;
  email?: string;
  /** A refusal not tied to one field (pair mismatch, operator-only). */
  general?: string;
}

/** The slice of a getProject response the identity editor reads. */
export interface GitIdentityProjectData {
  git_identity_name?: string | null;
  git_identity_email?: string | null;
  git_identity?: EffectiveGitIdentity | null;
}

export const GIT_IDENTITY_SOURCE_LABEL: Record<GitIdentitySource, string> = {
  project: "Project override",
  installation: "Installation default",
  fallback: "Fallback — installation default not configured",
};

export const GIT_IDENTITY_SETTINGS_PATH = "/settings/config";
export const GIT_IDENTITY_CLI_HINT = 'aq system config git-identity';
export const GIT_IDENTITY_SECTION_HINT =
  "The author and committer on every commit AQ makes for this project.";
export const GIT_IDENTITY_APPLIES_NOTE =
  "Changes apply to commits from sessions launched afterwards; existing history is never rewritten.";

const MAX_NAME_LENGTH = 200;
const MAX_EMAIL_LENGTH = 254;
// Characters Git strips from both ends of an ident field (ident.c crud()).
const GIT_CRUD = new Set([".", ",", ":", ";", "<", ">", '"', "\\", "'"]);
const CONTROL_CHARS = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;
const EMAIL_SHAPE = /^[^@\s<>]+@[^@\s<>]+$/;

function checkField(label: string, value: string, limit: number): string | null {
  const text = value.trim();
  if (!text) return `${label} must not be empty`;
  if ([...text].length > limit) return `${label} must be at most ${limit} characters`;
  if (CONTROL_CHARS.test(text)) return `${label} must be a single line without control characters`;
  if (text.includes("<") || text.includes(">")) return `${label} must not contain '<' or '>'`;
  if (GIT_CRUD.has(text[0]!) || GIT_CRUD.has(text[text.length - 1]!)) {
    return `${label} must not start or end with punctuation Git strips (. , : ; " ' \\)`;
  }
  return null;
}

export function validateGitName(value: string): string | null {
  return checkField("Name", value, MAX_NAME_LENGTH);
}

export function validateGitEmail(value: string): string | null {
  const error = checkField("Email", value, MAX_EMAIL_LENGTH);
  if (error) return error;
  const text = value.trim();
  if (/\s/.test(text) || !EMAIL_SHAPE.test(text)) {
    return "Email must be one address of the form name@domain";
  }
  return null;
}

/** Client-side errors for the form; empty while the project inherits. */
export function gitIdentityErrors(form: GitIdentityFormFields): GitIdentityFieldErrors {
  if (!form.git_identity_override) return {};
  const errors: GitIdentityFieldErrors = {};
  const name = validateGitName(form.git_identity_name);
  const email = validateGitEmail(form.git_identity_email);
  if (name) errors.name = name;
  if (email) errors.email = email;
  return errors;
}

export function hasGitIdentityErrors(errors: GitIdentityFieldErrors): boolean {
  return Boolean(errors.name || errors.email || errors.general);
}

/** What the project falls back to when it has no override of its own. */
export function inheritedGitIdentity(
  identity: EffectiveGitIdentity | null | undefined,
): { name: string; email: string; source: "installation" | "fallback" } | null {
  if (!identity) return null;
  if (identity.installation) return { ...identity.installation, source: "installation" };
  return { ...identity.fallback, source: "fallback" };
}

/**
 * Form fields for a project. Inheriting projects get the inherited pair
 * prefilled so "Override for this project" starts from what commits use now.
 */
export function gitIdentityFormFields(p: GitIdentityProjectData): GitIdentityFormFields {
  const name = p.git_identity_name ?? "";
  const email = p.git_identity_email ?? "";
  if (name && email) {
    return { git_identity_override: true, git_identity_name: name, git_identity_email: email };
  }
  const inherited = inheritedGitIdentity(p.git_identity);
  return {
    git_identity_override: false,
    git_identity_name: inherited?.name ?? "",
    git_identity_email: inherited?.email ?? "",
  };
}

/**
 * The identity part of an edit_project body. Both fields travel together;
 * an unchanged identity sends nothing so a save never clobbers it, and a
 * reset sends both as "" (the typed route drops nulls, so null cannot clear).
 */
export function gitIdentityPayload(
  saved: GitIdentityFormFields,
  form: GitIdentityFormFields,
): { git_identity_name?: string; git_identity_email?: string } {
  if (!form.git_identity_override) {
    return saved.git_identity_override ? { git_identity_name: "", git_identity_email: "" } : {};
  }
  const name = form.git_identity_name.trim();
  const email = form.git_identity_email.trim();
  if (
    saved.git_identity_override &&
    name === saved.git_identity_name.trim() &&
    email === saved.git_identity_email.trim()
  ) {
    return {};
  }
  return { git_identity_name: name, git_identity_email: email };
}

/**
 * Attribute an edit_project failure to the identity fields, or null when it
 * is not about them. The typed route's 422 envelope carries only `error`
 * (GitIdentityError's "name: …" / "email: …"); a `field` in the payload wins
 * when the daemon sends one.
 */
export function gitIdentitySaveError(err: unknown): GitIdentityFieldErrors | null {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  const message = raw.replace(/^API \d+:\s*/, "").trim();
  const payload = (err as { payload?: { field?: unknown; error_code?: unknown } } | null)?.payload;
  const field = typeof payload?.field === "string" ? payload.field.replace(/^git_identity_/, "") : "";
  const detail = message.replace(/^(name|email):\s*/, "");
  if (field === "name" || (!field && /^name:\s/.test(message))) return { name: `Name ${detail}` };
  if (field === "email" || (!field && /^email:\s/.test(message))) return { email: `Email ${detail}` };
  if (payload?.error_code === "invalid_git_identity" || /git identity/i.test(message)) {
    return { general: message };
  }
  return null;
}
