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

// A symbol key: anything but a letter, a digit, whitespace or the library's
// "+", "," and ">" separators.
const SYMBOL_KEY = /^[^\p{L}\p{N}\s+,>]$/u;

/**
 * The react-hotkeys-hook binding for a shortcut spec. The library rejects an
 * event whose Shift state differs from the binding's, but typing a symbol such
 * as "?" holds Shift on most layouts, so a combo ending in a symbol also binds
 * its Shift variant: the character typed is the shortcut, however the layout
 * reaches it. Letters and digits keep an exact Shift match, because Shift turns
 * "g" into "G" and "1" into "!" (which the library would still match to "1" by
 * its code).
 */
function hotkeyBinding(spec: string): string {
  return (
    spec
      // Accept legacy modifier-key notation as well as the library's modifier+key syntax.
      .replace(/\b(meta|ctrl|alt|shift)-/g, "$1+")
      .split(",")
      .flatMap((combo) => {
        const parts = combo.trim().toLowerCase().split("+");
        const symbol = SYMBOL_KEY.test(parts[parts.length - 1] ?? "") && !parts.includes("shift");
        return symbol ? [combo, `shift+${combo.trim()}`] : [combo];
      })
      .join(",")
  );
}

/**
 * Register a hotkey. The `key` string uses react-hotkeys-hook syntax with
 * `$mod` as a stand-in for the platform modifier (cmd on mac, ctrl elsewhere).
 * A key matches the character it types (`event.key`, so "[" or "/") or its
 * physical key's code name ("bracketleft"); a symbol such as "?" fires whether or
 * not the layout needs Shift to type it. Also feeds the cheat-sheet via the
 * context registry, which shows `key` as written.
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
    hotkeyBinding(expanded),
    (e) => {
      if (optsRef.current.when && !optsRef.current.when()) return;
      e.preventDefault();
      optsRef.current.onFire();
    },
    // Without useKey, react-hotkeys-hook 5 compares only the mapped event.code,
    // where "[" arrives as "bracketleft" and "/" as "slash", so a punctuation
    // binding could never fire. With it the library tries event.key first and
    // still falls back to the code.
    { enableOnFormTags: false, preventDefault: true, useKey: true },
  );
}

export function useCheatSheet(): Registered[] {
  return useContext(ListC);
}
