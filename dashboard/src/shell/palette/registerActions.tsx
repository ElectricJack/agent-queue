import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export interface PaletteAction {
  id: string;
  label: string;
  section?: string;
  keywords?: string[];
  run: () => void;
}

interface Ctx {
  actions: PaletteAction[];
  register: (a: PaletteAction) => () => void;
}

const C = createContext<Ctx | null>(null);

export function ActionRegistryProvider({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<PaletteAction[]>([]);
  const register = useCallback((a: PaletteAction) => {
    setActions((prev) => {
      const rest = prev.filter((x) => x.id !== a.id);
      return [...rest, a];
    });
    return () => setActions((prev) => prev.filter((x) => x.id !== a.id));
  }, []);
  const value = useMemo<Ctx>(() => ({ actions, register }), [actions, register]);
  return <C.Provider value={value}>{children}</C.Provider>;
}

export function useRegisterAction(a: PaletteAction): void {
  const ctx = useContext(C);
  useEffect(() => {
    if (!ctx) return;
    return ctx.register(a);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctx, a.id, a.label, a.section]);
}

export function useActions(): PaletteAction[] {
  return useContext(C)?.actions ?? [];
}
