/**
 * Writable server-backed dashboard documents — the write half of
 * docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md (§10).
 * DashboardStateProvider owns the one store; the bootstrap it runs and the
 * live invalidation in ws/useEventStream share its query keys.
 *
 * The daemon is authoritative from first use. A document renders its
 * namespace default until the server answers and the server's document after
 * that. Nothing here reads or writes browser storage, imports legacy values,
 * or keeps a fallback copy: when the server cannot be reached the document
 * reports ``unavailable``, keeps rendering the default, and refuses writes.
 *
 * Storage seams:
 * - the query cache holds the last *server-confirmed* document per address,
 *   replaced only by a strictly higher revision (rule R6), so any order or
 *   duplication of reads, write responses, refetches and events converges;
 * - a per-address overlay holds the optimistic value of queued writes, so a
 *   failed write falls back to the confirmed document rather than a
 *   client-side snapshot;
 * - a per-address queue serialises writes: each is an operation over the
 *   current value, sent with ``base_revision`` and re-applied to the server's
 *   document on ``revision_conflict``.
 */
import { createContext, useCallback, useContext, useSyncExternalStore } from "react";
import { queryOptions, useQuery, type QueryClient } from "@tanstack/react-query";
import {
  dashboardStatePut,
  dashboardStateReset,
  type DashboardStateListResponse,
  type DashboardStatePutRequest,
} from "./client";
import {
  DASHBOARD_STATE_BOOTSTRAP_KEY,
  DASHBOARD_STATE_NAMESPACES,
  dashboardStateDocumentKey,
  fetchDashboardStateBootstrap,
  fetchDashboardStateDocument,
  seedDashboardStateDocuments,
  type DashboardStateDocument,
  type DashboardStateNamespace,
} from "./dashboardState";

export type DocumentOf<N extends DashboardStateNamespace> = Extract<
  DashboardStateDocument,
  { namespace: N }
>;
export type ValueOf<N extends DashboardStateNamespace> = DocumentOf<N>["value"];
export type DocumentStatus = "loading" | "ready" | "unavailable";

function isProjectKeyed(namespace: DashboardStateNamespace): boolean {
  return DASHBOARD_STATE_NAMESPACES[namespace].subject === "project";
}

/** The server's namespace defaults (src/api/models/dashboard.py), shown until it answers. */
export const DEFAULT_VALUES: { readonly [N in DashboardStateNamespace]: ValueOf<N> } = {
  nav_organization: { folders: [], assignments: {}, project_order: [] },
  shell_preferences: {
    theme: "dark",
    pane_widths: {},
    right_surface: { width: 480, kind: null, activity_tab: "gates", pane: null },
    projects_section_open: true,
    agent_flock_collapsed: false,
    last_project_id: null,
  },
  command_center_preferences: { density: "comfortable" },
  command_center_project_view: {
    expanded_task_ids: [],
    expanded_finished_task_ids: [],
    manual_positions: {},
  },
  playbook_graph_view: { manual_positions: {} },
};

/** Consecutive revision conflicts after which a write gives up (contract §10.4). */
export const MAX_CONFLICTS = 3;
/** While the bootstrap is failing, retry it on this cadence. */
const UNAVAILABLE_RETRY_MS = 15_000;

/** The four daemon calls, injectable so tests can stand in a fake server. */
export interface DashboardStateTransport {
  list(): Promise<DashboardStateListResponse>;
  get(namespace: DashboardStateNamespace, subject: string | null): Promise<DashboardStateDocument>;
  put(request: DashboardStatePutRequest): Promise<DashboardStateDocument>;
  reset(namespace: DashboardStateNamespace, subject: string | null): Promise<DashboardStateDocument>;
}

export const sdkTransport: DashboardStateTransport = {
  list: () => fetchDashboardStateBootstrap(),
  get: (namespace, subject) => fetchDashboardStateDocument(namespace, subject),
  async put(body) {
    const { data } = await dashboardStatePut({ body, throwOnError: true });
    return data.document;
  },
  async reset(namespace, subject) {
    const { data } = await dashboardStateReset({ body: { namespace, subject }, throwOnError: true });
    return data.document;
  },
};

/** A write that kept losing the revision race; the server's document is now shown. */
export class DashboardStateConflictError extends Error {
  constructor(readonly current: DashboardStateDocument) {
    super(`dashboard state ${current.namespace} kept changing underneath this write`);
    this.name = "DashboardStateConflictError";
  }
}

/** The ``current`` document of a 409 ``revision_conflict`` body, if that is what failed. */
function conflictCurrent(error: unknown): DashboardStateDocument | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const { error_code: code, current } = payload as { error_code?: unknown; current?: unknown };
  if (code !== "revision_conflict" || typeof current !== "object" || current === null) return null;
  return current as DashboardStateDocument;
}

/** R6: a held document is replaced only by a strictly greater revision. */
function newer<D extends DashboardStateDocument>(held: D | undefined, incoming: D): D {
  return held !== undefined && held.revision >= incoming.revision ? held : incoming;
}

function address(namespace: DashboardStateNamespace, subject: string | null): string {
  return namespace + " " + (subject ?? "");
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

interface AddressMeta {
  /** Optimistic value of queued writes; ``null`` when nothing is queued. */
  overlay: { value: unknown } | null;
  /** Why the last write to this address failed; cleared by the next success. */
  error: Error | null;
}

const EMPTY_META: AddressMeta = { overlay: null, error: null };

interface Writer {
  tail: Promise<void>;
  pending: number;
}

export class DashboardStateStore {
  private readonly meta = new Map<string, AddressMeta>();
  private readonly listeners = new Set<() => void>();
  private readonly writers = new Map<string, Writer>();

  /** The bootstrap query: fetched once, refetched by useEventStream on reconnect. */
  readonly bootstrapOptions;

  constructor(
    private readonly client: QueryClient,
    private readonly transport: DashboardStateTransport,
  ) {
    this.bootstrapOptions = queryOptions({
      queryKey: DASHBOARD_STATE_BOOTSTRAP_KEY,
      queryFn: this.bootstrap,
      staleTime: Infinity,
      refetchInterval: (query) => (query.state.status === "error" ? UNAVAILABLE_RETRY_MS : false),
    });
  }

  /** Fetch every visible document and seed the per-address cache under R6. */
  private readonly bootstrap = async (): Promise<DashboardStateListResponse> => {
    const response = await this.transport.list();
    seedDashboardStateDocuments(this.client, response);
    return response;
  };

  readonly fetchDocument = async (
    namespace: DashboardStateNamespace,
    subject: string | null,
  ): Promise<DashboardStateDocument> => {
    const incoming = await this.transport.get(namespace, subject);
    return newer(this.confirmed(namespace, subject), incoming);
  };

  readonly subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly metaFor = (key: string): AddressMeta => this.meta.get(key) ?? EMPTY_META;

  private setMeta(key: string, patch: Partial<AddressMeta>): void {
    this.meta.set(key, { ...this.metaFor(key), ...patch });
    this.listeners.forEach((listener) => listener());
  }

  private confirmed(
    namespace: DashboardStateNamespace,
    subject: string | null,
  ): DashboardStateDocument | undefined {
    return this.client.getQueryData<DashboardStateDocument>(
      dashboardStateDocumentKey(namespace, subject),
    );
  }

  private adopt(document: DashboardStateDocument): void {
    const key = dashboardStateDocumentKey(document.namespace, document.subject);
    this.client.setQueryData<DashboardStateDocument>(key, (held) => newer(held, document));
  }

  /** The confirmed document, loading it first; throws while the server is unavailable. */
  private async load(
    namespace: DashboardStateNamespace,
    subject: string | null,
  ): Promise<DashboardStateDocument> {
    const held = this.confirmed(namespace, subject);
    if (held) return held;
    if (!isProjectKeyed(namespace)) {
      await this.client.fetchQuery(this.bootstrapOptions);
      const seeded = this.confirmed(namespace, subject);
      if (seeded) return seeded;
    }
    return this.client.fetchQuery({
      queryKey: dashboardStateDocumentKey(namespace, subject),
      queryFn: () => this.fetchDocument(namespace, subject),
      staleTime: Infinity,
    });
  }

  private refresh(namespace: DashboardStateNamespace, subject: string | null): void {
    void this.client.invalidateQueries({
      queryKey: dashboardStateDocumentKey(namespace, subject),
      exact: true,
    });
  }

  /**
   * Serialise ``task`` behind every earlier write to the same address. The
   * overlay shows ``preview`` immediately when the document is loaded; it is
   * dropped once the queue drains, leaving the confirmed document on screen.
   */
  private enqueue(
    namespace: DashboardStateNamespace,
    subject: string | null,
    preview: (value: unknown) => unknown,
    task: (writer: Writer, key: string) => Promise<void>,
  ): Promise<void> {
    const key = address(namespace, subject);
    let writer = this.writers.get(key);
    if (!writer) {
      writer = { tail: Promise.resolve(), pending: 0 };
      this.writers.set(key, writer);
    }
    const queue = writer;
    queue.pending += 1;
    const held = this.confirmed(namespace, subject);
    if (held) {
      const shown = this.metaFor(key).overlay;
      this.setMeta(key, { overlay: { value: preview(shown ? shown.value : held.value) } });
    }
    const run = queue.tail.then(async () => {
      try {
        await task(queue, key);
        this.setMeta(key, { error: null });
      } catch (error) {
        this.setMeta(key, { error: error instanceof Error ? error : new Error(String(error)) });
        throw error;
      } finally {
        queue.pending -= 1;
        if (queue.pending === 0) this.setMeta(key, { overlay: null });
      }
    });
    queue.tail = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  update<N extends DashboardStateNamespace>(
    namespace: N,
    subject: string | null,
    op: (current: ValueOf<N>) => ValueOf<N>,
  ): Promise<void> {
    const apply = op as (current: unknown) => unknown;
    return this.enqueue(namespace, subject, apply, async (writer, key) => {
      let current = await this.load(namespace, subject);
      for (let conflicts = 0; ; ) {
        const next = apply(current.value);
        if (sameValue(next, current.value)) return;
        if (writer.pending === 1) this.setMeta(key, { overlay: { value: next } });
        try {
          this.adopt(
            await this.transport.put({
              namespace,
              subject,
              base_revision: current.revision,
              value: next as DashboardStatePutRequest["value"],
            }),
          );
          return;
        } catch (error) {
          const server = conflictCurrent(error);
          if (!server) {
            this.refresh(namespace, subject);
            throw error;
          }
          conflicts += 1;
          if (conflicts >= MAX_CONFLICTS) {
            // Give up visibly: the server's document replaces the pending one.
            this.client.setQueryData(dashboardStateDocumentKey(namespace, subject), server);
            throw new DashboardStateConflictError(server);
          }
          this.adopt(server);
          current = server;
        }
      }
    });
  }

  reset(namespace: DashboardStateNamespace, subject: string | null): Promise<void> {
    return this.enqueue(namespace, subject, () => DEFAULT_VALUES[namespace], async () => {
      try {
        this.adopt(await this.transport.reset(namespace, subject));
      } catch (error) {
        this.refresh(namespace, subject);
        throw error;
      }
    });
  }
}

export const DashboardStateStoreContext = createContext<DashboardStateStore | null>(null);

export interface DashboardDocumentState<N extends DashboardStateNamespace> {
  status: DocumentStatus;
  /** The server's value; the namespace default while loading or unavailable. */
  value: ValueOf<N>;
  revision: number;
  exists: boolean;
  /** Why the last write failed, if it did; the value shown is the server's. */
  error: Error | null;
  /** Replace the whole value. */
  write(next: ValueOf<N>): Promise<void>;
  /** Apply an operation to the current value; re-applied if the document moved on. */
  update(op: (current: ValueOf<N>) => ValueOf<N>): Promise<void>;
  reset(): Promise<void>;
}

/** One document with its write path (contract §10.2). */
export function useDashboardDocumentState<N extends DashboardStateNamespace>(
  namespace: N,
  subject?: string | null,
): DashboardDocumentState<N> {
  const store = useContext(DashboardStateStoreContext);
  if (!store) throw new Error("dashboard state used outside DashboardStateProvider");
  const target = subject ?? null;
  const projectKeyed = isProjectKeyed(namespace);
  const bootstrap = useQuery({ ...store.bootstrapOptions, enabled: !projectKeyed });
  const document = useQuery({
    queryKey: dashboardStateDocumentKey(namespace, target),
    queryFn: () => store.fetchDocument(namespace, target),
    staleTime: Infinity,
    enabled: projectKeyed ? target !== null : bootstrap.isSuccess,
  });
  const key = address(namespace, target);
  const meta = useSyncExternalStore(
    store.subscribe,
    () => store.metaFor(key),
    () => store.metaFor(key),
  );
  const confirmed = document.data as DocumentOf<N> | undefined;
  const status: DocumentStatus = confirmed
    ? "ready"
    : document.isError || (!projectKeyed && bootstrap.isError)
      ? "unavailable"
      : "loading";
  const value = (
    meta.overlay ? meta.overlay.value : (confirmed?.value ?? DEFAULT_VALUES[namespace])
  ) as ValueOf<N>;

  const update = useCallback(
    (op: (current: ValueOf<N>) => ValueOf<N>) => store.update(namespace, target, op),
    [store, namespace, target],
  );
  const write = useCallback((next: ValueOf<N>) => update(() => next), [update]);
  const reset = useCallback(() => store.reset(namespace, target), [store, namespace, target]);

  return {
    status,
    value,
    revision: confirmed?.revision ?? 0,
    exists: confirmed?.exists ?? false,
    error: meta.error,
    write,
    update,
    reset,
  };
}
