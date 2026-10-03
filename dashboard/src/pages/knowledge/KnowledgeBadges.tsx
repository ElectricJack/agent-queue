/**
 * Text badges for knowledge facets. Each one names its facet for assistive
 * technology ("Lifecycle: retired") and shows only the value, so kind,
 * lifecycle and verification read as three separate statements and never
 * depend on colour alone. Same shape as `StatusBadge`.
 */
import type {
  KnowledgeCategory,
  KnowledgeLifecycle,
  KnowledgeListItemView,
  KnowledgeVerification,
  RecordKind,
} from "./model";

const BASE = "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium";

const KIND_COLORS: Record<RecordKind, string> = {
  task: "bg-sky-500/10 text-sky-300",
  knowledge: "bg-violet-500/10 text-violet-300",
};

const LIFECYCLE_COLORS: Record<KnowledgeLifecycle, string> = {
  active: "bg-green-500/10 text-green-400",
  retired: "bg-gray-500/10 text-gray-400",
};

const VERIFICATION_COLORS: Record<KnowledgeVerification, string> = {
  unverified: "bg-yellow-500/10 text-yellow-400",
  verified: "bg-green-500/10 text-green-400",
  disputed: "bg-red-500/10 text-red-400",
};

type Facet = "kind" | "category" | "lifecycle" | "verification" | "authority" | "freshness";

const FACET_LABELS: Record<Facet, string> = {
  kind: "Kind",
  category: "Category",
  lifecycle: "Lifecycle",
  verification: "Verification",
  authority: "Authority",
  freshness: "Freshness",
};

function Badge({ facet, value, colors, title }: { facet: Facet; value: string; colors: string; title?: string }) {
  return (
    <span data-badge={facet} title={title} className={`${BASE} ${colors}`}>
      <span className="sr-only">{FACET_LABELS[facet]}: </span>
      {value}
    </span>
  );
}

export function KindBadge({ kind }: { kind: RecordKind }) {
  return <Badge facet="kind" value={kind} colors={KIND_COLORS[kind]} />;
}

export function CategoryBadge({ category }: { category: KnowledgeCategory }) {
  return <Badge facet="category" value={category} colors="bg-indigo-500/10 text-indigo-300" />;
}

export function LifecycleBadge({ lifecycle }: { lifecycle: KnowledgeLifecycle }) {
  return <Badge facet="lifecycle" value={lifecycle} colors={LIFECYCLE_COLORS[lifecycle]} />;
}

export function VerificationBadge({ verification }: { verification: KnowledgeVerification }) {
  return <Badge facet="verification" value={verification} colors={VERIFICATION_COLORS[verification]} />;
}

export function AuthorityBadge() {
  return <Badge facet="authority" value="authoritative" colors="bg-emerald-500/10 text-emerald-300" />;
}

export function StaleBadge({ reason }: { reason?: string | null }) {
  return <Badge facet="freshness" value="stale" colors="bg-orange-500/10 text-orange-400" title={reason ?? undefined} />;
}

type BadgeFacets = Pick<KnowledgeListItemView, "kind" | "category" | "lifecycle" | "verification" | "authoritative" | "stale">;

/** The standard row a card or a detail header shows. */
export function KnowledgeBadgeRow({ item, staleReason }: { item: BadgeFacets; staleReason?: string | null }) {
  return (
    <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
      <KindBadge kind={item.kind} />
      <CategoryBadge category={item.category} />
      <LifecycleBadge lifecycle={item.lifecycle} />
      <VerificationBadge verification={item.verification} />
      {item.authoritative && <AuthorityBadge />}
      {item.stale && <StaleBadge reason={staleReason} />}
    </span>
  );
}
