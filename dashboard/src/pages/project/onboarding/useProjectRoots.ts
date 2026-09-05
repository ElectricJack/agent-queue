/**
 * Configured project roots (design §3.2) as the wizard consumes them.
 *
 * The daemon-side `list_project_roots` command belongs to the
 * project-root-configuration work package and has not landed yet, so there is
 * no client function to call: until it does, the wizard can only truthfully
 * report that no roots are available, which renders the "No project roots
 * configured" empty state with its Settings link. Replace the body of
 * `useProjectRoots` with the real query when the endpoint exists — the
 * wizard depends only on the `ProjectRootsSource` shape.
 */

export interface ProjectRootSummary {
  id: string;
  label: string;
  /** Operator-facing path (may be abbreviated, e.g. `~/dev`). */
  displayPath: string;
  readable: boolean;
  writable: boolean;
}

export type ProjectRootsSource =
  | { status: "loading" }
  | { status: "ready"; roots: ProjectRootSummary[] }
  | { status: "error"; message: string };

const NO_ROOTS: ProjectRootsSource = { status: "ready", roots: [] };

export function useProjectRoots(): ProjectRootsSource {
  return NO_ROOTS;
}
