import type { SourceState } from "./state";

const PROJECT_ID = /^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/;

/** A non-authoritative default for the editable Project identity step (§3.4). */
export function deriveProjectIdentity(source: SourceState): { projectName: string; projectId: string } | null {
  const name = sourceName(source);
  if (!name) return null;
  return { projectName: name, projectId: slugifyProjectId(name) };
}

/** Mirrors the onboarding contract's URL-safe project-id shape for immediate feedback. */
export function projectIdError(projectId: string, existingProjectIds: readonly string[]): string | null {
  if (!PROJECT_ID.test(projectId)) {
    return "Project ID must be URL-safe: lower-case letters, numbers, dots, underscores, and hyphens.";
  }
  if (existingProjectIds.includes(projectId)) return "This project ID is already in use.";
  return null;
}

export function slugifyProjectId(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[._-]+|[._-]+$/g, "")
    .replace(/-+/g, "-");
}

function sourceName(source: SourceState): string | null {
  switch (source.mode) {
    case "link":
      return pathName(source.relativePath);
    case "init":
      return source.directoryName || null;
    case "github_clone":
      return source.githubRepository?.name ?? pathName(source.githubUrl.replace(/\.git$/, ""));
    case null:
      return null;
  }
}

function pathName(value: string | null): string | null {
  if (!value) return null;
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? null;
}
