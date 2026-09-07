import { useCallback, useEffect, useRef, useState } from "react";
import {
  NAV_ORGANIZATION_CHANGED,
  NAV_ORGANIZATION_STORAGE_KEY,
  type NavOrganization,
  saveOrganization,
  storedOrganization,
} from "./navOrganization";

/**
 * The rail's project organization, kept in sync with localStorage and with any
 * other rail in this document or another tab.
 */
export function useNavOrganization(): {
  organization: NavOrganization;
  update: (next: (org: NavOrganization) => NavOrganization) => void;
} {
  const [organization, setOrganization] = useState<NavOrganization>(storedOrganization);
  // Written by the state updater, flushed after commit: saving notifies every
  // other rail synchronously, which must not happen during a render.
  const pending = useRef<NavOrganization | null>(null);

  useEffect(() => {
    const reread = () =>
      setOrganization((current) => {
        const stored = storedOrganization();
        return JSON.stringify(stored) === JSON.stringify(current) ? current : stored;
      });
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === NAV_ORGANIZATION_STORAGE_KEY) reread();
    };
    window.addEventListener(NAV_ORGANIZATION_CHANGED, reread);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(NAV_ORGANIZATION_CHANGED, reread);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  useEffect(() => {
    const next = pending.current;
    if (!next) return;
    pending.current = null;
    saveOrganization(next);
  });

  const update = useCallback((next: (org: NavOrganization) => NavOrganization) => {
    setOrganization((current) => {
      const updated = next(current);
      if (updated === current) return current;
      pending.current = updated;
      return updated;
    });
  }, []);

  return { organization, update };
}
