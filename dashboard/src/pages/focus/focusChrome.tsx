import { createContext, useContext, useEffect, type ReactNode } from "react";

export interface FocusChrome {
  title: string;
  /** Where "Open in full dashboard" leads; omitted on the home page. */
  fullHref?: string | null;
}

const Ctx = createContext<(chrome: FocusChrome) => void>(() => {});

export function FocusChromeProvider({ onChange, children }: { onChange: (chrome: FocusChrome) => void; children: ReactNode }) {
  return <Ctx.Provider value={onChange}>{children}</Ctx.Provider>;
}

/** A focus page names its header title and its full-dashboard counterpart. */
// eslint-disable-next-line react-refresh/only-export-components
export function useFocusChrome({ title, fullHref = null }: FocusChrome) {
  const set = useContext(Ctx);
  useEffect(() => set({ title, fullHref }), [set, title, fullHref]);
}
