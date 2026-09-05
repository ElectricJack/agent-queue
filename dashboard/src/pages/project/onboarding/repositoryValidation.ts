/** Return a human-readable local validation error, or `null` when valid. */
export function directoryNameError(value: string): string | null {
  if (!value.trim()) return "Enter a directory name.";
  if (value.includes("/") || value.includes("\\")) return "Enter a single directory name without path separators.";
  if (value === "." || value === "..") return "Enter a directory name other than . or ...";
  if (value.includes("\0")) return "Directory names cannot contain NUL characters.";
  return null;
}
