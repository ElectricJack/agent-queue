/** Typed directory-browser boundary backed by the generated daemon client. */
import { browseProjectRoot as browseProjectRootCommand } from "../../../api/client";
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
  rootId: string,
  relativePath: string,
): Promise<ProjectRootBrowseResult> {
  const { data } = await browseProjectRootCommand({
    body: { root_id: rootId, relative_path: relativePath || undefined },
    throwOnError: true,
  });
  return {
    relativePath: data.relative_path ?? "",
    entries: (data.entries ?? []).map((entry) => ({
      name: entry.name,
      relativePath: entry.relative_path,
      isDirectory: entry.is_directory ?? false,
      isGitRepository: entry.is_git_repository ?? false,
      selectable: entry.selectable ?? false,
    })),
  };
}
