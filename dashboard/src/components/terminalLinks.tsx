import { createContext, useContext, type ReactNode } from "react";

/**
 * Where a terminal link leads. "interactive" (the default) opens the agents
 * page, which attaches a live terminal; "watch" opens the read-only focus
 * session. Focus routes provide "watch": they never attach (mobile dashboard §4).
 */
export type TerminalLinkMode = "interactive" | "watch";

const Mode = createContext<TerminalLinkMode>("interactive");

export function TerminalLinkModeProvider({ mode, children }: { mode: TerminalLinkMode; children: ReactNode }) {
  return <Mode.Provider value={mode}>{children}</Mode.Provider>;
}

// A paired provider/hook module follows the existing pane-store convention.
// eslint-disable-next-line react-refresh/only-export-components
export function useTerminalLinkMode(): TerminalLinkMode {
  return useContext(Mode);
}
