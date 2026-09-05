/**
 * Read-only directory-browser boundary. The wizard integration task replaces
 * this adapter with the generated client command; keeping the UI dependent on
 * this narrow contract makes the browser independently testable.
 */
export interface ProjectRootBrowseEntry {
  name: string;
  relativePath: string;
  isDirectory: boolean;
  isGitRepository: boolean;
  selectable: boolean;
}

export interface ProjectRootBrowseResult {
  relativePath: string;
  entries: ProjectRootBrowseEntry[];
}

export async function browseProjectRoot(
  _rootId: string,
  _relativePath: string,
): Promise<ProjectRootBrowseResult> {
  void _rootId;
  void _relativePath;
  throw new Error("Project-root browsing is not connected yet");
}
