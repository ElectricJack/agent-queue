/**
 * A display-only parser for the GitHub URL forms accepted by the wizard.
 * The original text remains in state and is what the server receives.
 */
export function githubRepositoryDisplay(value: string): { owner: string; name: string } | null {
  const raw = value.trim().replace(/[?#].*$/, "").replace(/\.git$/, "");
  const match = raw.match(
    /^(?:https?:\/\/github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)?([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/?$/,
  );
  if (!match) return null;
  return { owner: match[1]!, name: match[2]! };
}
