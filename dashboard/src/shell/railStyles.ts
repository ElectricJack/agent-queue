/** Shared row styling for the left rail's navigation entries. */
export function linkClass(active: boolean): string {
  return `flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
    active
      ? "bg-indigo-500/15 text-indigo-200"
      : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
  }`;
}
