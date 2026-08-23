import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { expandMod } from "./usePlatform";

export interface ShortcutOpts {
  label: string;
  onFire: () => void;
  section?: string;
  when?: () => boolean;
}

interface Registered {
  id: string;
  key: string;
  opts: ShortcutOpts;
}

interface Ctx {
  registered: Registered[];
  register: (r: Registered) => () => void;
}

const C = createContext<Ctx | null>(null);

export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const [registered, setRegistered] = useState<Registered[]>([]);
  const register = useCallback((r: Registered) => {
    setRegistered((prev) => [...prev, r]);
    return () => setRegistered((prev) => prev.filter((x) => x.id !== r.id));
  }, []);
  const value = useMemo<Ctx>(() => ({ registered, register }), [registered, register]);
  return <C.Provider value={value}>{children}</C.Provider>;
}

let nextId = 0;
function makeId(): string {
  return `sc-${++nextId}`;
}

/**
 * Register a hotkey. The `key` string uses react-hotkeys-hook syntax with
 * `$mod` as a stand-in for the platform modifier (cmd on mac, ctrl elsewhere).
 * Also feeds the cheat-sheet via the context registry.
 */
export function useShortcut(key: string, opts: ShortcutOpts): void {
  const ctx = useContext(C);
  const expanded = useMemo(() => expandMod(key), [key]);
  const id = useMemo(makeId, []);
  // Keep opts fresh in a ref so we don't re-register on every render — new
  // objects on every render otherwise thrash the context and cause
  // useEffect to loop indefinitely.
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const {
    label: labelDep,
    section: sectionDep,
  } = opts;

  useEffect(() => {
    if (!ctx) return;
    return ctx.register({
      id,
      key: expanded,
      opts: {
        label: labelDep,
        section: sectionDep,
        onFire: () => optsRef.current.onFire(),
        when: () => (optsRef.current.when ? optsRef.current.when() : true),
      },
    });
  }, [ctx, id, expanded, labelDep, sectionDep]);

  useHotkeys(
    expanded,
    (e) => {
      if (optsRef.current.when && !optsRef.current.when()) return;
      e.preventDefault();
      optsRef.current.onFire();
    },
    { enableOnFormTags: false, preventDefault: true },
  );
}

export function useCheatSheet(): Registered[] {
  const ctx = useContext(C);
  return ctx?.registered ?? [];
}
