import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { FOCUS_ROOT } from "./routes";

/**
 * Back within the app, or — for a link opened cold, whose history entry is the
 * router's "default" first one — to the focus home rather than off the app
 * (mobile dashboard §4.1).
 */
export function useFocusBack(): () => void {
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(() => {
    if (location.key !== "default") navigate(-1);
    else navigate(FOCUS_ROOT, { replace: true });
  }, [navigate, location.key]);
}
