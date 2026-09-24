/** A selection may focus xterm only while the operator is not editing elsewhere. */
export function canFocusTerminal(): boolean {
  if (document.querySelector("dialog[open], [role='dialog'][aria-modal='true']")) return false;
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return true;
  if (active.closest("dialog[open], [role='dialog']")) return false;
  // An already focused xterm textarea is the terminal we are switching from.
  if (active.closest("[data-interactive-terminal]")) return true;
  return !active.matches("input, textarea, [contenteditable]:not([contenteditable='false'])")
    && !active.closest("[contenteditable]:not([contenteditable='false'])");
}
