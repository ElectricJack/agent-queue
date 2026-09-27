import type { RefObject } from "react";
import { focusableIn, useFocusTrap as useSharedFocusTrap } from "../../../hooks/useFocusTrap";

export { focusableIn };

/** The wizard's trap: shared implementation, focus restoration left to the wizard. */
export function useFocusTrap(container: RefObject<HTMLElement | null>, active: boolean) {
  useSharedFocusTrap(container, active, { restoreFocus: false });
}
