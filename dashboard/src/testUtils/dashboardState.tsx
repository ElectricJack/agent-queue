/**
 * An in-memory stand-in for the daemon's dashboard-state commands, for tests.
 *
 * It keeps the server's rules that matter to the dashboard: the owner of a
 * user document comes from the transport (the request never names it),
 * revisions increase by one per accepted write or reset, a stale
 * `base_revision` is refused with the same 409 body the client interceptor
 * surfaces, and the bootstrap synthesizes every global namespace.
 */
import { QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { DashboardStatePutRequest } from "../api/client";
import { DashboardStateProvider } from "../api/DashboardStateProvider";
import {
  DASHBOARD_STATE_NAMESPACES,
  type DashboardStateDocument,
  type DashboardStateNamespace,
} from "../api/dashboardState";
import { DEFAULT_VALUES, type DashboardStateTransport } from "../api/dashboardStateStore";

export const LOCAL_OPERATOR = "human:local-operator";

const NAMESPACES = Object.keys(DASHBOARD_STATE_NAMESPACES) as DashboardStateNamespace[];

interface Row {
  revision: number;
  value: unknown;
  updated_at: number;
}

export interface FakeCall {
  op: "list" | "get" | "put" | "reset";
  owner: string;
  body?: unknown;
}

interface Address {
  owner?: string;
  subject?: string | null;
}

export interface FakeDashboardStateServer {
  /** One dashboard's connection, authenticated as `owner`. */
  transport(owner?: string): DashboardStateTransport;
  /** Every request in arrival order, with the owner the server derived. */
  calls: FakeCall[];
  /** Fail every request with `error` until called again with `null`. */
  failWith(error: Error | null): void;
  /** Hold every response until the returned function is called. */
  hold(): () => void;
  /** A write from another dashboard of `owner` (unconditional). */
  write(namespace: DashboardStateNamespace, value: unknown, at?: Address): DashboardStateDocument;
  document(namespace: DashboardStateNamespace, at?: Address): DashboardStateDocument;
}

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export function createFakeDashboardStateServer(): FakeDashboardStateServer {
  const rows = new Map<string, Row>();
  const calls: FakeCall[] = [];
  let failure: Error | null = null;
  let gate: Promise<void> | null = null;
  let clock = 1_000;

  const scopeOf = (namespace: DashboardStateNamespace) => DASHBOARD_STATE_NAMESPACES[namespace].scope;
  const ownerOf = (namespace: DashboardStateNamespace, owner: string) =>
    scopeOf(namespace) === "workspace" ? "" : owner;
  const rowKey = (namespace: DashboardStateNamespace, owner: string, subject: string | null) =>
    [scopeOf(namespace), ownerOf(namespace, owner), namespace, subject ?? ""].join("|");

  function document(
    namespace: DashboardStateNamespace,
    { owner = LOCAL_OPERATOR, subject = null }: Address = {},
  ): DashboardStateDocument {
    const row = rows.get(rowKey(namespace, owner, subject));
    return {
      scope: scopeOf(namespace),
      owner_id: ownerOf(namespace, owner),
      namespace,
      subject,
      revision: row?.revision ?? 0,
      exists: row !== undefined && row.value !== null,
      value: clone(row && row.value !== null ? row.value : DEFAULT_VALUES[namespace]),
      updated_at: row ? row.updated_at : null,
    } as DashboardStateDocument;
  }

  function commit(
    namespace: DashboardStateNamespace,
    owner: string,
    subject: string | null,
    value: unknown,
  ) {
    const key = rowKey(namespace, owner, subject);
    const revision = (rows.get(key)?.revision ?? 0) + 1;
    rows.set(key, { revision, value: value === null ? null : clone(value), updated_at: ++clock });
    return document(namespace, { owner, subject });
  }

  async function receive(op: FakeCall["op"], owner: string, body?: unknown) {
    calls.push({ op, owner, body: body === undefined ? undefined : clone(body) });
    if (gate) await gate;
    if (failure) throw failure;
  }

  return {
    calls,
    failWith(error) {
      failure = error;
    },
    hold() {
      let release!: () => void;
      gate = new Promise((resolve) => {
        release = resolve;
      });
      return () => {
        gate = null;
        release();
      };
    },
    write(namespace, value, { owner = LOCAL_OPERATOR, subject = null }: Address = {}) {
      return commit(namespace, owner, subject, value);
    },
    document,
    transport(owner = LOCAL_OPERATOR): DashboardStateTransport {
      return {
        async list() {
          await receive("list", owner);
          const globals = NAMESPACES.filter((ns) => DASHBOARD_STATE_NAMESPACES[ns].subject === "none")
            .map((ns) => document(ns, { owner }));
          const projectViews = [...rows.keys()]
            .map((key) => key.split("|") as [string, string, DashboardStateNamespace, string])
            .filter(([scope, rowOwner, ns]) =>
              DASHBOARD_STATE_NAMESPACES[ns].subject === "project"
              && (scope === "workspace" || rowOwner === owner))
            .map(([, , ns, subject]) => document(ns, { owner, subject }));
          return { success: true, owner_id: owner, documents: [...globals, ...projectViews] };
        },
        async get(namespace, subject) {
          await receive("get", owner, { namespace, subject });
          return document(namespace, { owner, subject });
        },
        async put(body: DashboardStatePutRequest) {
          await receive("put", owner, body);
          const subject = body.subject ?? null;
          const current = document(body.namespace, { owner, subject });
          if (body.base_revision != null && body.base_revision !== current.revision) {
            throw Object.assign(new Error("API 409: revision conflict"), {
              payload: {
                success: false,
                error_code: "revision_conflict",
                error: "revision conflict",
                current,
              },
            });
          }
          return commit(body.namespace, owner, subject, body.value);
        },
        async reset(namespace, subject) {
          await receive("reset", owner, { namespace, subject });
          return commit(namespace, owner, subject, null);
        },
      };
    },
  };
}

export function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/** A DashboardStateProvider wired to `server`; render it inside a QueryClientProvider. */
export function TestDashboardState({
  server,
  owner = LOCAL_OPERATOR,
  children,
}: {
  server: FakeDashboardStateServer;
  owner?: string;
  children: ReactNode;
}) {
  return <DashboardStateProvider transport={server.transport(owner)}>{children}</DashboardStateProvider>;
}
