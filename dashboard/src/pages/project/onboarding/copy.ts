import type { SourceMode, StepId } from "./state";

export const STEP_TITLES: Record<StepId, string> = {
  source: "Choose source",
  repository: "Choose repository",
  identity: "Project identity",
  options: "Options",
  review: "Review and create",
};

export const SOURCE_MODE_COPY: Record<SourceMode, { label: string; description: string }> = {
  link: {
    label: "Existing local repository",
    description: "Link a Git repository that already lives beneath a configured project root.",
  },
  init: {
    label: "New repository",
    description: "Initialize a new Git repository beneath a project root, optionally with a GitHub remote.",
  },
  github_clone: {
    label: "Clone from GitHub",
    description: "Clone a repository you can reach through the daemon host's GitHub session.",
  },
};
