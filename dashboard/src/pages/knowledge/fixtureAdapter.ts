/**
 * An in-memory `KnowledgeAdapter` for tests, scenarios and local review of
 * the Knowledge components while main has no knowledge routes.
 *
 * It keeps the server rules the components depend on: allowed actions come
 * from the (fixture) server per viewer and record, an update must present the
 * observed revision token and a moved head answers `conflict` with the current
 * token, a replayed idempotency key returns the original revision, redacted
 * revisions are tombstones, and an endpoint the viewer may not read carries no
 * title. It is a presentation fixture: nothing in it is an authority rule.
 */
import { KnowledgeAdapterError, type KnowledgeAdapter } from "./adapter";
import {
  DEFAULT_KNOWLEDGE_FILTERS,
  type KnowledgeAction,
  type KnowledgeCategory,
  type KnowledgeChangeKind,
  type KnowledgeDetailView,
  type KnowledgeDiffBlock,
  type KnowledgeDiffView,
  type KnowledgeHistoryEntryView,
  type KnowledgeHistoryPage,
  type KnowledgeLifecycle,
  type KnowledgeLinkView,
  type KnowledgeListFilters,
  type KnowledgeListItemView,
  type KnowledgeListPage,
  type KnowledgeRevisionRef,
  type KnowledgeScopeView,
  type KnowledgeSourceView,
  type KnowledgeUpdateInput,
  type KnowledgeUpdateResult,
  type KnowledgeVerification,
  type TaskKnowledgeView,
} from "./model";

export type FixturePersona = "worker" | "supervisor";

export interface FixtureCall {
  op: "list" | "show" | "history" | "diff" | "update" | "taskKnowledge";
  args: unknown[];
}

export interface KnowledgeFixtureAdapter extends KnowledgeAdapter {
  /** Every request in arrival order. */
  calls: FixtureCall[];
  /** Reject every request with `error` until called again with `null`. */
  failWith(error: Error | null): void;
  /** Hold every response until the returned function is called. */
  hold(): () => void;
  /** Another writer lands a revision, so an editor's observed token goes stale. */
  simulateExternalEdit(recordId: string, patch?: { title?: string; body?: string }): KnowledgeRevisionRef;
  /** Record ids in list order, for tests. */
  recordIds(): string[];
  readonly persona: FixturePersona;
}

export interface FixtureOptions {
  /** Who is looking: decides the allowed actions the fixture server returns. */
  persona?: FixturePersona;
  pageSize?: number;
  /** "Now" for staleness, RFC3339 UTC. */
  now?: string;
}

interface Revision extends KnowledgeRevisionRef {
  createdAt: string;
  actorId: string;
  changeKind: KnowledgeChangeKind;
  changeReason: string | null;
  verification: KnowledgeVerification;
  /** Redacted revisions have no snapshot. */
  snapshot: {
    title: string;
    body: string;
    summary: string | null;
    category: KnowledgeCategory;
    tags: string[];
  } | null;
  contentSha256: string | null;
}

interface Record_ {
  recordId: string;
  alias: string;
  scope: KnowledgeScopeView;
  /** The actor whose unverified finding this is; a worker edits only its own. */
  ownerActor: string;
  protection: "none" | "protected";
  lifecycle: KnowledgeLifecycle;
  retirementReason: string | null;
  successorId: string | null;
  authority: { reviewId: string | null; reason: string | null; grantedBy: string; grantedAt: string } | null;
  lastVerifiedAt: string | null;
  lastVerifiedBy: string | null;
  validFrom: string | null;
  validUntil: string | null;
  recheckAt: string | null;
  updatedAt: string;
  sources: KnowledgeSourceView[];
  links: KnowledgeLinkView[];
  revisions: Revision[];
}

const WORKER = "session:worker-a";
const SUPERVISOR = "supervisor:agent-queue";
const OPERATOR = "human:local-operator";
const PROJECT: KnowledgeScopeView = { kind: "project", projectId: "agent-queue" };

const ALIASES = [
  "kn-0a1b2c3d4e5f60718293a4b5c6d7e8f9",
  "kn-1b2c3d4e5f60718293a4b5c6d7e8f90a",
  "kn-2c3d4e5f60718293a4b5c6d7e8f90a1b",
  "kn-3d4e5f60718293a4b5c6d7e8f90a1b2c",
  "kn-4e5f60718293a4b5c6d7e8f90a1b2c3d",
  "kn-5f60718293a4b5c6d7e8f90a1b2c3d4e",
] as const;

const IDS = [
  "6f1d2c3b-0000-4000-8000-000000000001",
  "6f1d2c3b-0000-4000-8000-000000000002",
  "6f1d2c3b-0000-4000-8000-000000000003",
  "6f1d2c3b-0000-4000-8000-000000000004",
  "6f1d2c3b-0000-4000-8000-000000000005",
  "6f1d2c3b-0000-4000-8000-000000000006",
] as const;

const sha = (seed: string) => {
  // A stable 64-hex label for display and equality in tests; not a real digest.
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h.toString(16).padStart(8, "0").repeat(8);
};

function revision(
  recordIndex: number,
  sequence: number,
  fields: Omit<Revision, "revisionId" | "sequence" | "contentSha256">,
): Revision {
  const revisionId = `rev-${recordIndex + 1}-${sequence}`;
  return {
    revisionId,
    sequence,
    contentSha256: fields.snapshot ? sha(revisionId + fields.snapshot.title + fields.snapshot.body) : null,
    ...fields,
  };
}

const link = (
  linkId: string,
  type: KnowledgeLinkView["type"],
  direction: KnowledgeLinkView["direction"],
  endpoint: KnowledgeLinkView["endpoint"],
  pinnedRevision: KnowledgeRevisionRef | null,
  resolution: KnowledgeLinkView["resolution"],
): KnowledgeLinkView => ({
  linkId, version: 1, type, direction, endpoint, pinnedRevision, resolution, edgeDomain: "informational",
});

function seed(): Record_[] {
  const postgresBody = [
    "# PostgreSQL is the only supported database",
    "",
    "SQLAlchemy Core on PostgreSQL only. There is no SQLite path and no",
    "`dialect.name` branch; `tests/test_sqlite_removal.py` is the ratchet.",
  ].join("\n");
  const outageBodyV1 = [
    "# Scheduler outage 2026-09-28",
    "",
    "A `json` column has no equality operator, so a whole-row `DISTINCT` over",
    "the table failed at plan time and the scheduler tick aborted.",
  ].join("\n");
  const outageBodyV2 = outageBodyV1 + [
    "",
    "",
    "## Fix",
    "",
    "Every JSON column is `JSONB`; `tests/test_migration_json_columns.py` pins it.",
  ].join("\n");

  return [
    {
      recordId: IDS[0], alias: ALIASES[0], scope: PROJECT, ownerActor: SUPERVISOR, protection: "protected",
      lifecycle: "active", retirementReason: null, successorId: null,
      authority: {
        reviewId: "rev-fleet-cascade", reason: "Bound by approved design review, revision 2",
        grantedBy: OPERATOR, grantedAt: "2026-10-01T18:40:00.000000Z",
      },
      lastVerifiedAt: "2026-10-01T18:30:00.000000Z", lastVerifiedBy: SUPERVISOR,
      validFrom: "2026-09-01T00:00:00.000000Z", validUntil: null, recheckAt: "2027-03-01T00:00:00.000000Z",
      updatedAt: "2026-10-01T18:30:00.000000Z",
      sources: [
        { sourceId: "src-1a", type: "review", label: "review rev-fleet-cascade r2", href: "/reviews/rev-fleet-cascade", evidence: "retained" },
        { sourceId: "src-1b", type: "git", label: "git 78e556fb tests/test_sqlite_removal.py", href: null, evidence: "retained" },
      ],
      links: [
        link("lnk-1a", "references", "outgoing", { kind: "task", id: "fresh-cascade-88", title: "Integrate fresh-cascade-88" }, null, "current"),
        link("lnk-1b", "motivated_by", "incoming", { kind: "task", id: "swift-grove-52", title: "K10 parallel slice: Knowledge UI components" }, null, "current"),
      ],
      revisions: [
        revision(0, 1, {
          createdAt: "2026-09-30T09:00:00.000000Z", actorId: SUPERVISOR, changeKind: "create", changeReason: null,
          verification: "unverified",
          snapshot: { title: "PostgreSQL is the only supported database", body: postgresBody, summary: "Postgres only; no SQLite path.", category: "fact", tags: ["database", "postgres"] },
        }),
        revision(0, 2, {
          createdAt: "2026-10-01T18:30:00.000000Z", actorId: SUPERVISOR, changeKind: "verify", changeReason: "Verified against the approved design",
          verification: "verified",
          snapshot: { title: "PostgreSQL is the only supported database", body: postgresBody, summary: "Postgres only; no SQLite path.", category: "fact", tags: ["database", "postgres"] },
        }),
      ],
    },
    {
      recordId: IDS[1], alias: ALIASES[1], scope: PROJECT, ownerActor: WORKER, protection: "none",
      lifecycle: "active", retirementReason: null, successorId: null, authority: null,
      lastVerifiedAt: null, lastVerifiedBy: null, validFrom: null, validUntil: null, recheckAt: null,
      updatedAt: "2026-09-29T11:15:00.000000Z",
      sources: [
        { sourceId: "src-2a", type: "task", label: "task fresh-cascade-88", href: "/tasks/fresh-cascade-88", evidence: "retained" },
        { sourceId: "src-2b", type: "git", label: "git 5a8ba1ec src/database/tables.py", href: null, evidence: "retained" },
        { sourceId: "src-2c", type: "url", label: "https://www.postgresql.org/docs/current/datatype-json.html", href: null, evidence: "unretained" },
      ],
      links: [
        link("lnk-2a", "references", "outgoing", { kind: "knowledge", id: IDS[0], title: "PostgreSQL is the only supported database" }, { revisionId: "rev-1-2", sequence: 2 }, "pinned"),
        link("lnk-2b", "contradicts", "outgoing", { kind: "knowledge", id: IDS[3], title: null }, null, "unauthorized"),
        link("lnk-2c", "motivated_by", "incoming", { kind: "task", id: "fresh-cascade-88", title: "Integrate fresh-cascade-88" }, null, "current"),
      ],
      revisions: [
        revision(1, 1, {
          createdAt: "2026-09-28T16:02:00.000000Z", actorId: WORKER, changeKind: "create", changeReason: null,
          verification: "unverified",
          snapshot: { title: "Scheduler outage 2026-09-28: JSON columns without equality", body: outageBodyV1, summary: null, category: "incident", tags: ["postgres", "scheduler"] },
        }),
        revision(1, 2, {
          createdAt: "2026-09-29T10:40:00.000000Z", actorId: WORKER, changeKind: "update", changeReason: "Record the fix",
          verification: "unverified",
          snapshot: { title: "Scheduler outage 2026-09-28: JSON columns without equality", body: outageBodyV2, summary: null, category: "incident", tags: ["postgres", "scheduler"] },
        }),
        revision(1, 3, {
          createdAt: "2026-09-29T11:15:00.000000Z", actorId: WORKER, changeKind: "update", changeReason: "Add summary",
          verification: "unverified",
          snapshot: { title: "Scheduler outage 2026-09-28: JSON columns without equality", body: outageBodyV2, summary: "json has no equality operator; use JSONB everywhere.", category: "incident", tags: ["postgres", "scheduler"] },
        }),
      ],
    },
    {
      recordId: IDS[2], alias: ALIASES[2], scope: PROJECT, ownerActor: SUPERVISOR, protection: "none",
      lifecycle: "retired", retirementReason: "Superseded by the Postgres-only test DSN", successorId: IDS[0], authority: null,
      lastVerifiedAt: null, lastVerifiedBy: null, validFrom: null, validUntil: "2026-09-15T00:00:00.000000Z", recheckAt: null,
      updatedAt: "2026-09-20T08:00:00.000000Z",
      sources: [],
      links: [
        link("lnk-3a", "supersedes", "incoming", { kind: "knowledge", id: IDS[0], title: "PostgreSQL is the only supported database" }, null, "current"),
      ],
      revisions: [
        revision(2, 1, {
          createdAt: "2026-08-01T12:00:00.000000Z", actorId: SUPERVISOR, changeKind: "create", changeReason: null,
          verification: "unverified",
          snapshot: { title: "Decision: SQLite retained for local tests", body: "Local tests may use SQLite while Postgres is provisioned.", summary: null, category: "decision", tags: ["testing"] },
        }),
        revision(2, 2, {
          createdAt: "2026-09-20T08:00:00.000000Z", actorId: SUPERVISOR, changeKind: "retire", changeReason: "Superseded by the Postgres-only test DSN",
          verification: "unverified",
          snapshot: { title: "Decision: SQLite retained for local tests", body: "Local tests may use SQLite while Postgres is provisioned.", summary: null, category: "decision", tags: ["testing"] },
        }),
      ],
    },
    {
      recordId: IDS[3], alias: ALIASES[3], scope: PROJECT, ownerActor: SUPERVISOR, protection: "protected",
      lifecycle: "active", retirementReason: null, successorId: null, authority: null,
      lastVerifiedAt: "2026-07-10T09:00:00.000000Z", lastVerifiedBy: SUPERVISOR,
      validFrom: null, validUntil: null, recheckAt: "2026-09-01T00:00:00.000000Z",
      updatedAt: "2026-09-25T14:00:00.000000Z",
      sources: [
        { sourceId: "src-4a", type: "artifact", label: "artifact 3c9f… (resource-gating.md)", href: null, evidence: "unavailable" },
      ],
      links: [],
      revisions: [
        revision(3, 1, {
          createdAt: "2026-07-01T09:00:00.000000Z", actorId: SUPERVISOR, changeKind: "create", changeReason: null,
          verification: "unverified",
          snapshot: { title: "Policy: never run the full suite mid-task", body: "One broader run at the end; the whole suite belongs to CI.", summary: null, category: "policy", tags: ["testing", "resources"] },
        }),
        revision(3, 2, {
          createdAt: "2026-07-10T09:00:00.000000Z", actorId: SUPERVISOR, changeKind: "verify", changeReason: null,
          verification: "verified",
          snapshot: { title: "Policy: never run the full suite mid-task", body: "One broader run at the end; the whole suite belongs to CI.", summary: null, category: "policy", tags: ["testing", "resources"] },
        }),
        revision(3, 3, {
          createdAt: "2026-09-25T14:00:00.000000Z", actorId: WORKER, changeKind: "dispute", changeReason: "aq test slots changed the cost model",
          verification: "disputed",
          snapshot: { title: "Policy: never run the full suite mid-task", body: "One broader run at the end; the whole suite belongs to CI.", summary: null, category: "policy", tags: ["testing", "resources"] },
        }),
      ],
    },
    {
      recordId: IDS[4], alias: ALIASES[4], scope: PROJECT, ownerActor: WORKER, protection: "none",
      lifecycle: "active", retirementReason: null, successorId: null, authority: null,
      lastVerifiedAt: null, lastVerifiedBy: null, validFrom: null, validUntil: null, recheckAt: null,
      updatedAt: "2026-09-26T10:00:00.000000Z",
      sources: [],
      links: [],
      revisions: [
        revision(4, 1, {
          createdAt: "2026-09-26T09:00:00.000000Z", actorId: WORKER, changeKind: "create", changeReason: null,
          verification: "unverified", snapshot: null,
        }),
        revision(4, 2, {
          createdAt: "2026-09-26T10:00:00.000000Z", actorId: OPERATOR, changeKind: "redact", changeReason: null,
          verification: "unverified",
          snapshot: { title: "Procedure: restart after an update", body: "Use `aq restart --no-dashboard`; it preserves agent sessions.", summary: null, category: "procedure", tags: ["operations"] },
        }),
      ],
    },
    {
      recordId: IDS[5], alias: ALIASES[5], scope: PROJECT, ownerActor: WORKER, protection: "none",
      lifecycle: "active", retirementReason: null, successorId: null, authority: null,
      lastVerifiedAt: null, lastVerifiedBy: null, validFrom: null, validUntil: null, recheckAt: null,
      updatedAt: "2026-09-27T10:00:00.000000Z",
      sources: [
        { sourceId: "src-6a", type: "git", label: "git c1b4d282 docs/guides/worker-pools.md", href: null, evidence: "retained" },
      ],
      links: [],
      revisions: [
        revision(5, 1, {
          createdAt: "2026-09-27T10:00:00.000000Z", actorId: WORKER, changeKind: "create", changeReason: null,
          verification: "unverified",
          snapshot: { title: "Reference: worker pools guide", body: "See `docs/guides/worker-pools.md` for pool sizing and the frontier.", summary: "Pointer to the pools guide.", category: "reference", tags: ["pools"] },
        }),
      ],
    },
  ];
}

const current = (record: Record_): Revision => record.revisions[record.revisions.length - 1]!;
const ref = (rev: KnowledgeRevisionRef): KnowledgeRevisionRef => ({ revisionId: rev.revisionId, sequence: rev.sequence });

/** The fixture server's view of what each persona may do; a presentation stand-in, not policy. */
function allowedActions(record: Record_, persona: FixturePersona): KnowledgeAction[] {
  const actions: KnowledgeAction[] = ["history", "create_task"];
  const active = record.lifecycle === "active";
  if (persona === "supervisor") {
    if (active) actions.push("edit", "link", "retire");
    else actions.push("restore");
    return actions;
  }
  if (!active) return actions;
  actions.push("link");
  const head = current(record);
  const ownUnverified = record.ownerActor === WORKER && head.verification === "unverified"
    && record.protection === "none" && record.authority === null;
  actions.push(ownUnverified ? "edit" : "propose_correction");
  return actions;
}

function isStale(record: Record_, now: string): { stale: boolean; reason: string | null } {
  if (record.recheckAt && record.recheckAt <= now) return { stale: true, reason: "Recheck date has passed" };
  if (record.validUntil && record.validUntil <= now) return { stale: true, reason: "Validity has ended" };
  return { stale: false, reason: null };
}

function diffLines(from: string, to: string): KnowledgeDiffBlock[] {
  const a = from.split("\n");
  const b = to.split("\n");
  const n = a.length;
  const m = b.length;
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i]![j] = a[i] === b[j]
        ? table[i + 1]![j + 1]! + 1
        : Math.max(table[i + 1]![j]!, table[i]![j + 1]!);
    }
  }
  const blocks: KnowledgeDiffBlock[] = [];
  const push = (op: KnowledgeDiffBlock["op"], text: string) => {
    const last = blocks[blocks.length - 1];
    if (last && last.op === op) last.text += `\n${text}`;
    else blocks.push({ op, text });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { push("equal", a[i]!); i++; j++; }
    else if (table[i + 1]![j]! >= table[i]![j + 1]!) { push("removed", a[i]!); i++; }
    else { push("added", b[j]!); j++; }
  }
  while (i < n) push("removed", a[i++]!);
  while (j < m) push("added", b[j++]!);
  return blocks;
}

export function createKnowledgeFixtureAdapter(options: FixtureOptions = {}): KnowledgeFixtureAdapter {
  const persona = options.persona ?? "worker";
  const pageSize = options.pageSize ?? 25;
  const now = options.now ?? "2026-10-02T12:00:00.000000Z";
  const records = seed();
  const calls: FixtureCall[] = [];
  const receipts = new Map<string, KnowledgeUpdateResult>();
  let failure: Error | null = null;
  let gate: Promise<void> | null = null;
  let clock = Date.parse(now);

  const byId = (recordId: string): Record_ => {
    const record = records.find((r) => r.recordId === recordId || r.alias === recordId);
    // Denied and nonexistent read the same: no title, no count.
    if (!record) throw new KnowledgeAdapterError("not_found", "record not found");
    return record;
  };

  async function admit<T>(op: FixtureCall["op"], args: unknown[], run: () => T): Promise<T> {
    calls.push({ op, args });
    if (gate) await gate;
    if (failure) throw failure;
    return run();
  }

  const listItem = (record: Record_): KnowledgeListItemView => {
    const head = current(record);
    const snapshot = head.snapshot ?? { title: "(redacted)", summary: null, category: "note" as const, tags: [] };
    const staleness = isStale(record, now);
    return {
      kind: "knowledge",
      recordId: record.recordId,
      alias: record.alias,
      title: snapshot.title,
      summary: snapshot.summary,
      category: snapshot.category,
      lifecycle: record.lifecycle,
      verification: head.verification,
      authoritative: record.authority !== null && head.verification === "verified" && record.lifecycle === "active",
      stale: staleness.stale,
      staleReason: staleness.reason,
      tags: snapshot.tags,
      updatedAt: record.updatedAt,
      current: ref(head),
    };
  };

  const matches = (item: KnowledgeListItemView, filters: KnowledgeListFilters): boolean => {
    if (filters.category && item.category !== filters.category) return false;
    if (filters.lifecycle && item.lifecycle !== filters.lifecycle) return false;
    if (filters.verification && item.verification !== filters.verification) return false;
    const words = filters.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    if (words.length === 0) return true;
    const haystack = [item.alias, item.title, item.summary ?? "", ...item.tags].join(" ").toLocaleLowerCase();
    return words.every((word) => haystack.includes(word));
  };

  const detail = (record: Record_, viewed: Revision): KnowledgeDetailView => {
    const head = current(record);
    const snapshot = viewed.snapshot ?? head.snapshot;
    const staleness = isStale(record, now);
    const successor = record.successorId ? records.find((r) => r.recordId === record.successorId) ?? null : null;
    return {
      kind: "knowledge",
      recordId: record.recordId,
      alias: record.alias,
      scope: record.scope,
      title: snapshot?.title ?? record.alias,
      body: viewed.snapshot?.body ?? null,
      summary: viewed.snapshot?.summary ?? null,
      category: snapshot?.category ?? "note",
      tags: viewed.snapshot?.tags ?? [],
      lifecycle: record.lifecycle,
      retirementReason: record.retirementReason,
      successor: successor
        ? { recordId: successor.recordId, alias: successor.alias, title: current(successor).snapshot?.title ?? null }
        : null,
      verification: {
        state: viewed.verification,
        lastVerifiedAt: record.lastVerifiedAt,
        lastVerifiedBy: record.lastVerifiedBy,
        authority: record.authority && viewed.revisionId === head.revisionId && head.verification === "verified" && record.lifecycle === "active"
          ? { kind: "policy", ...record.authority }
          : null,
      },
      freshness: {
        validFrom: record.validFrom, validUntil: record.validUntil, recheckAt: record.recheckAt,
        stale: staleness.stale, staleReason: staleness.reason,
      },
      viewed: {
        ...ref(viewed),
        isCurrent: viewed.revisionId === head.revisionId,
        createdAt: viewed.createdAt,
        actorId: viewed.actorId,
        changeKind: viewed.changeKind,
        contentSha256: viewed.contentSha256,
      },
      current: ref(head),
      allowedActions: allowedActions(record, persona),
      protection: record.protection,
      redacted: viewed.snapshot === null,
      sources: viewed.snapshot ? record.sources : [],
      links: record.links,
    };
  };

  const stamp = (): string => {
    clock += 60_000;
    return new Date(clock).toISOString().replace("Z", "000Z");
  };

  const append = (record: Record_, fields: Omit<Revision, "revisionId" | "sequence" | "contentSha256" | "createdAt">): Revision => {
    const sequence = current(record).sequence + 1;
    const index = records.indexOf(record);
    const next = revision(index, sequence, { ...fields, createdAt: stamp() });
    record.revisions.push(next);
    record.updatedAt = next.createdAt;
    return next;
  };

  return {
    persona,
    calls,
    failWith(error) { failure = error; },
    hold() {
      let release!: () => void;
      gate = new Promise<void>((resolve) => { release = () => { gate = null; resolve(); }; });
      return release;
    },
    recordIds: () => records.map((r) => r.recordId),
    simulateExternalEdit(recordId, patch = {}) {
      const record = byId(recordId);
      const head = current(record);
      const base = head.snapshot ?? { title: record.alias, body: "", summary: null, category: "note" as const, tags: [] };
      const next = append(record, {
        actorId: "session:worker-b",
        changeKind: "update",
        changeReason: "Concurrent edit",
        verification: "unverified",
        snapshot: { ...base, title: patch.title ?? `${base.title} (revised)`, body: patch.body ?? `${base.body}\n\nRevised elsewhere.` },
      });
      return ref(next);
    },

    list: (filters, cursor) => admit("list", [filters, cursor], (): KnowledgeListPage => {
      const effective = { ...DEFAULT_KNOWLEDGE_FILTERS, ...filters };
      const all = records.map(listItem).filter((item) => matches(item, effective))
        .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt) || a.recordId.localeCompare(b.recordId));
      const start = cursor ? Number.parseInt(cursor, 10) || 0 : 0;
      const items = all.slice(start, start + pageSize);
      return { items, nextCursor: start + pageSize < all.length ? String(start + pageSize) : null };
    }),

    show: (recordId, revisionId) => admit("show", [recordId, revisionId], () => {
      const record = byId(recordId);
      const viewed = revisionId ? record.revisions.find((r) => r.revisionId === revisionId) : current(record);
      // A pinned revision resolves exactly or is unavailable; it never falls back to current.
      if (!viewed) throw new KnowledgeAdapterError("revision_unavailable", "revision unavailable");
      return detail(record, viewed);
    }),

    history: (recordId, cursor) => admit("history", [recordId, cursor], (): KnowledgeHistoryPage => {
      const record = byId(recordId);
      const head = current(record);
      const entries: KnowledgeHistoryEntryView[] = [...record.revisions].reverse().map((rev) => ({
        ...ref(rev),
        createdAt: rev.createdAt,
        actorId: rev.actorId,
        changeKind: rev.changeKind,
        changeReason: rev.changeReason,
        verification: rev.verification,
        redacted: rev.snapshot === null,
        isCurrent: rev.revisionId === head.revisionId,
      }));
      const start = cursor ? Number.parseInt(cursor, 10) || 0 : 0;
      return {
        entries: entries.slice(start, start + pageSize),
        nextCursor: start + pageSize < entries.length ? String(start + pageSize) : null,
      };
    }),

    diff: (recordId, fromRevisionId, toRevisionId) => admit("diff", [recordId, fromRevisionId, toRevisionId], (): KnowledgeDiffView => {
      const record = byId(recordId);
      const from = record.revisions.find((r) => r.revisionId === fromRevisionId);
      const to = record.revisions.find((r) => r.revisionId === toRevisionId);
      if (!from || !to) throw new KnowledgeAdapterError("revision_unavailable", "revision unavailable");
      // Diff needs both payloads readable; a tombstone on either side is explicit.
      if (!from.snapshot || !to.snapshot) throw new KnowledgeAdapterError("revision_redacted", "revision redacted");
      return { from: ref(from), to: ref(to), blocks: diffLines(from.snapshot.body, to.snapshot.body) };
    }),

    update: (input: KnowledgeUpdateInput) => admit("update", [input], (): KnowledgeUpdateResult => {
      const record = byId(input.recordId);
      const receipt = receipts.get(input.idempotencyKey);
      // A completed key answers with its original result before the token is re-tested.
      if (receipt) return receipt.outcome === "updated" ? { outcome: "replayed", revision: receipt.revision } : receipt;
      if (!allowedActions(record, persona).includes("edit")) {
        return { outcome: "forbidden", message: "This record takes proposals, not direct edits." };
      }
      const head = current(record);
      if (input.ifRevision !== head.revisionId) return { outcome: "conflict", current: ref(head) };
      if (!input.draft.title.trim()) return { outcome: "invalid", message: "Title is required." };
      const base = head.snapshot;
      const same = base
        && base.title === input.draft.title && base.body === input.draft.body
        && (base.summary ?? null) === (input.draft.summary ?? null)
        && base.category === input.draft.category
        && base.tags.join("\u0000") === [...input.draft.tags].sort().join("\u0000");
      if (same) {
        const result: KnowledgeUpdateResult = { outcome: "unchanged" };
        receipts.set(input.idempotencyKey, result);
        return result;
      }
      const next = append(record, {
        actorId: WORKER,
        changeKind: "update",
        changeReason: input.draft.reason || null,
        // Any content change drops to unverified and clears authority.
        verification: "unverified",
        snapshot: {
          title: input.draft.title, body: input.draft.body, summary: input.draft.summary,
          category: input.draft.category, tags: [...new Set(input.draft.tags)].sort(),
        },
      });
      record.authority = null;
      const result: KnowledgeUpdateResult = { outcome: "updated", revision: ref(next) };
      receipts.set(input.idempotencyKey, result);
      return result;
    }),

    taskKnowledge: (taskId) => admit("taskKnowledge", [taskId], (): TaskKnowledgeView => {
      if (taskId !== "swift-grove-52") return { taskId, citations: [], links: [] };
      const postgres = byId(IDS[0]);
      const outage = byId(IDS[1]);
      return {
        taskId,
        citations: [
          {
            citationId: "cit-1", recordId: postgres.recordId, alias: postgres.alias,
            title: current(postgres).snapshot?.title ?? null,
            revision: ref(current(postgres)), isCurrent: true, resolution: "pinned",
            citedAt: "2026-10-02T09:00:00.000000Z",
          },
          {
            citationId: "cit-2", recordId: outage.recordId, alias: outage.alias,
            title: outage.revisions[1]!.snapshot?.title ?? null,
            revision: ref(outage.revisions[1]!), isCurrent: false, resolution: "pinned",
            citedAt: "2026-10-02T09:05:00.000000Z",
          },
          {
            citationId: "cit-3", recordId: IDS[3], alias: ALIASES[3], title: null,
            revision: { revisionId: "rev-4-2", sequence: 2 }, isCurrent: false, resolution: "unauthorized",
            citedAt: "2026-10-02T09:10:00.000000Z",
          },
        ],
        links: [
          link("lnk-t1", "motivated_by", "outgoing", { kind: "knowledge", id: postgres.recordId, title: current(postgres).snapshot?.title ?? null }, null, "current"),
          link("lnk-t2", "references", "outgoing", { kind: "knowledge", id: outage.recordId, title: current(outage).snapshot?.title ?? null }, ref(outage.revisions[1]!), "pinned"),
          link("lnk-t3", "references", "outgoing", { kind: "knowledge", id: IDS[3], title: null }, null, "unauthorized"),
        ],
      };
    }),
  };
}
