import { useCallback, useEffect, useRef, useState } from "react";
import {
  dashboardStateGet,
  dashboardStatePut,
  type NavOrganizationDocument,
} from "../api/client";
import {
  EMPTY_ORGANIZATION,
  parseOrganization,
  type NavOrganization,
} from "./navOrganization";

type Status = "loading" | "ready" | "unavailable";
type Operation = {
  apply: (organization: NavOrganization) => NavOrganization;
  resolve: () => void;
};

const MAX_CONFLICT_RETRIES = 3;

function documentValue(document: NavOrganizationDocument): NavOrganization {
  return parseOrganization(document.value);
}

function conflictDocument(error: unknown): NavOrganizationDocument | null {
  if (!(error instanceof Error)) return null;
  const payload = (error as Error & { payload?: unknown }).payload;
  if (!payload || typeof payload !== "object") return null;
  const response = payload as { error_code?: unknown; current?: unknown };
  if (response.error_code !== "revision_conflict") return null;
  const current = response.current;
  if (!current || typeof current !== "object" || (current as { namespace?: unknown }).namespace !== "nav_organization") {
    return null;
  }
  return current as NavOrganizationDocument;
}

/**
 * The shared Projects-rail organization. CAS updates are kept in one queue:
 * each operation writes against the revision produced by its predecessor and
 * replays over a conflict response instead of overwriting another dashboard.
 */
export function useNavOrganization(): {
  organization: NavOrganization;
  status: Status;
  error: string | null;
  update: (next: (org: NavOrganization) => NavOrganization) => Promise<void>;
} {
  const [organization, setOrganization] = useState<NavOrganization>(EMPTY_ORGANIZATION);
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const statusRef = useRef<Status>("loading");
  const visibleRef = useRef<NavOrganization>(EMPTY_ORGANIZATION);
  const documentRef = useRef<{ value: NavOrganization; revision: number }>({
    value: EMPTY_ORGANIZATION,
    // A synthetic -1 lets the initial revision-0 server document be adopted
    // while every subsequent adoption obeys the strict R6 revision rule.
    revision: -1,
  });
  const queueRef = useRef<Operation[]>([]);
  const flushingRef = useRef(false);
  const mountedRef = useRef(true);

  const show = useCallback((next: NavOrganization) => {
    visibleRef.current = next;
    if (mountedRef.current) setOrganization(next);
  }, []);

  const setCurrentStatus = useCallback((next: Status) => {
    statusRef.current = next;
    if (mountedRef.current) setStatus(next);
  }, []);

  const adopt = useCallback((document: NavOrganizationDocument) => {
    const value = documentValue(document);
    if (document.revision <= documentRef.current.revision) return documentRef.current;
    documentRef.current = { value, revision: document.revision };
    return documentRef.current;
  }, []);

  const replay = useCallback((base: NavOrganization, operations: readonly Operation[]) => (
    operations.reduce((value, operation) => operation.apply(value), base)
  ), []);

  const refresh = useCallback(async () => {
    const { data } = await dashboardStateGet({
      body: { namespace: "nav_organization" },
      throwOnError: true,
    });
    const document = data.document;
    if (document.namespace !== "nav_organization") throw new Error("Unexpected dashboard state document");
    return adopt(document);
  }, [adopt]);

  const flush = useCallback(async () => {
    if (flushingRef.current || statusRef.current !== "ready") return;
    flushingRef.current = true;
    try {
      while (queueRef.current.length && statusRef.current === "ready") {
        const operation = queueRef.current.shift()!;
        let current = documentRef.current;
        let saved = false;
        let transportUnavailable = false;

        for (let attempt = 0; attempt < MAX_CONFLICT_RETRIES; attempt += 1) {
          const value = operation.apply(current.value);
          try {
            const { data } = await dashboardStatePut({
              body: {
                namespace: "nav_organization",
                base_revision: current.revision,
                value,
              },
              throwOnError: true,
            });
            if (data.document.namespace !== "nav_organization") throw new Error("Unexpected dashboard state document");
            current = adopt(data.document);
            saved = true;
            break;
          } catch (cause) {
            const conflict = conflictDocument(cause);
            if (!conflict) {
              setError(cause instanceof Error ? cause.message : "Unable to save project organization");
              try {
                current = await refresh();
              } catch {
                setCurrentStatus("unavailable");
                transportUnavailable = true;
              }
              break;
            }
            current = adopt(conflict);
            // Replay on the conflict value so another dashboard is never overwritten.
            show(replay(current.value, [operation, ...queueRef.current]));
          }
        }

        if (!saved) {
          // A failed operation must not remain as a tab-only optimistic value.
          if (transportUnavailable) {
            show(current.value);
            operation.resolve();
            for (const queued of queueRef.current.splice(0)) queued.resolve();
            break;
          }
          show(replay(current.value, queueRef.current));
          operation.resolve();
          continue;
        }

        if (!queueRef.current.length) show(current.value);
        operation.resolve();
      }
    } finally {
      flushingRef.current = false;
      if (queueRef.current.length && statusRef.current === "ready") void flush();
    }
  }, [adopt, refresh, replay, setCurrentStatus, show]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh()
      .then((document) => {
        setCurrentStatus("ready");
        show(replay(document.value, queueRef.current));
        void flush();
      })
      .catch((cause) => {
        setCurrentStatus("unavailable");
        setError(cause instanceof Error ? cause.message : "Project organization is unavailable");
        for (const queued of queueRef.current.splice(0)) queued.resolve();
      });
    return () => {
      mountedRef.current = false;
    };
  }, [flush, refresh, replay, setCurrentStatus, show]);

  const update = useCallback((next: (org: NavOrganization) => NavOrganization) => new Promise<void>((resolve) => {
    if (statusRef.current === "unavailable") {
      setError("Project organization is unavailable");
      resolve();
      return;
    }
    const operation = { apply: next, resolve };
    queueRef.current.push(operation);
    if (statusRef.current === "ready") {
      show(next(visibleRef.current));
      void flush();
    }
  }), [flush, show]);

  return { organization, status, error, update };
}
