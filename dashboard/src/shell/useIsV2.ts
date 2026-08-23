/** v2 shell is now the only shell. Kept as a hook so future callers can
 *  still key off it (e.g. a shell-swap kill-switch) without changing
 *  every import. Returns `true` unconditionally — dropping `?v2=1` from
 *  a URL no longer tears down the shell tree. */
export function useIsV2(): boolean {
  return true;
}
