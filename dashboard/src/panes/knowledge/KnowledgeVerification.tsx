import type { ReactNode } from "react";
import { AuthorityBadge, StaleBadge, VerificationBadge } from "../../pages/knowledge/KnowledgeBadges";
import { formatKnowledgeTimestamp, type KnowledgeDetailView } from "../../pages/knowledge/model";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-gray-200">{children}</dd>
    </div>
  );
}

/**
 * Verification, authority and freshness, each stated separately: retrieval
 * age is not verification, and a stale record is still whatever its
 * lifecycle says it is.
 */
export default function KnowledgeVerification({ detail }: { detail: KnowledgeDetailView }) {
  const { verification, freshness } = detail;
  return (
    <div className="space-y-4 text-xs">
      <section aria-label="Verification" className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-200">Verification</h3>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
          <Field label="State"><VerificationBadge verification={verification.state} /></Field>
          <Field label="Last verified">
            {verification.lastVerifiedAt
              ? <>{formatKnowledgeTimestamp(verification.lastVerifiedAt)} by {verification.lastVerifiedBy ?? "unknown"}</>
              : "Never"}
          </Field>
        </dl>
      </section>
      <section aria-label="Authority" className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-200">Authority</h3>
        {verification.authority ? (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
            <Field label="Grant"><AuthorityBadge /> <span className="text-gray-400">{verification.authority.kind}</span></Field>
            <Field label="Bound review">{verification.authority.reviewId ?? "None (explicit grant)"}</Field>
            <Field label="Granted">{formatKnowledgeTimestamp(verification.authority.grantedAt)} by {verification.authority.grantedBy}</Field>
            <Field label="Reason">{verification.authority.reason ?? "—"}</Field>
          </dl>
        ) : (
          <p className="text-gray-500">No active authority grant{detail.viewed.isCurrent ? "" : " applies to a historical revision"}.</p>
        )}
      </section>
      <section aria-label="Freshness" className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-200">Freshness</h3>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
          <Field label="Valid from">{formatKnowledgeTimestamp(freshness.validFrom)}</Field>
          <Field label="Valid until">{formatKnowledgeTimestamp(freshness.validUntil)}</Field>
          <Field label="Recheck at">{formatKnowledgeTimestamp(freshness.recheckAt)}</Field>
          <Field label="Status">
            {freshness.stale ? <><StaleBadge reason={freshness.staleReason} /> <span className="text-gray-400">{freshness.staleReason}</span></> : "Fresh"}
          </Field>
        </dl>
      </section>
    </div>
  );
}
