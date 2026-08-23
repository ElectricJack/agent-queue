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

type RegisterFn = (r: Registered) => () => void;

const RegisterC = createContext<RegisterFn | null>(null);
const ListC = createContext<Registered[]>([]);

export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const [registered, setRegistered] = useState<Registered[]>([]);
  const register = useCallback<RegisterFn>((r) => {
    setRegistered((prev) => [...prev, r]);
    return () => setRegistered((prev) => prev.filter((x) => x.id !== r.id));
  }, []);
  return (
    <RegisterC.Provider value={register}>
      <ListC.Provider value={registered}>{children}</ListC.Provider>
    </RegisterC.Provider>
  );
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
  const register = useContext(RegisterC);
  const expanded = useMemo(() => expandMod(key), [key]);
  const id = useMemo(makeId, []);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const {
    label: labelDep,
    section: sectionDep,
  } = opts;

  useEffect(() => {
    if (!register) return;
    return register({
      id,
      key: expanded,
      opts: {
        label: labelDep,
        section: sectionDep,
        onFire: () => optsRef.current.onFire(),
        when: () => (optsRef.current.when ? optsRef.current.when() : true),
      },
    });
  }, [register, id, expanded, labelDep, sectionDep]);

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
  return useContext(ListC);
}
