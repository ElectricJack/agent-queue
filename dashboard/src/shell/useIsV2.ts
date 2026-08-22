import { useSearchParams } from "react-router-dom";

/** Reads the `?v2=1` flag — when true, the app mounts AppShellV2. */
export function useIsV2(): boolean {
  const [params] = useSearchParams();
  return params.get("v2") === "1";
}
