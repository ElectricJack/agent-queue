import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

interface Ctx {
  open: boolean;
  setOpen: (b: boolean) => void;
  toggle: () => void;
}

const C = createContext<Ctx | null>(null);

export function PaletteStateProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const value = useMemo<Ctx>(
    () => ({ open, setOpen, toggle: () => setOpen(!open) }),
    [open],
  );
  return <C.Provider value={value}>{children}</C.Provider>;
}

export function usePaletteState(): Ctx {
  const v = useContext(C);
  if (!v) throw new Error("usePaletteState called outside PaletteStateProvider");
  return v;
}
