import type { AskPreview } from "../../api/client";

export function DecisionPreview({ preview, stale, pending, onConfirm }: {
  preview: AskPreview; stale: boolean; pending: boolean; onConfirm: () => void;
}) {
  const domain = preview.packet.domain as Record<string, unknown>;
  const context = preview.packet.context as { choices?: { key: string; effect: string }[] };
  const intent = preview.packet.intent as { option_key: string; overall_note?: string };
  const feedback = (domain.feedback ?? []) as { feedback_key: string; original_text: string;
    original_revision: number; original_author: string; disposition: string; acceptance_text: string;
    required_before_release: boolean; original_anchor?: unknown }[];
  return <section aria-label="Decision preview" className="space-y-3 rounded border border-indigo-700 p-3">
    <h3 className="font-semibold">Review this decision</h3>
    {domain.review_revision != null && <p>Exact revision {String(domain.review_revision)}</p>}
    <p>{context.choices?.find(c => c.key === intent.option_key)?.effect}</p>
    {domain.effective_option != null && <p>Result: {String(domain.effective_option).replace(/_/g, " ")}</p>}
    <p className="break-all text-xs text-gray-400">Content: {String(domain.content_sha256 ?? "source-bound")}<br />Packet: {preview.packet_sha256}</p>
    {intent.overall_note && <p className="whitespace-pre-wrap">Overall note: {intent.overall_note}</p>}
    <h4 className="font-medium">Full feedback mapping ({feedback.length})</h4>
    <ul className="space-y-3">{feedback.map(item => <li key={item.feedback_key} className="border-t border-gray-700 pt-2">
      <p className="whitespace-pre-wrap">{item.original_text}</p>
      <p className="text-xs">{item.feedback_key} · revision {item.original_revision} · {item.original_author}</p>
      {item.original_anchor != null && <pre className="whitespace-pre-wrap text-xs">{JSON.stringify(item.original_anchor)}</pre>}
      <p>{item.disposition.replace(/_/g, " ")}{item.required_before_release && " · required before release"}</p>
      <p className="whitespace-pre-wrap">Acceptance: {item.acceptance_text}</p>
    </li>)}</ul>
    {stale && <p role="alert" className="text-amber-200">Preview is stale. Refresh and prepare again; feedback or the source may have changed.</p>}
    <button type="button" disabled={stale || pending} onClick={onConfirm}
      className="min-h-11 rounded bg-indigo-700 px-4 py-2 disabled:opacity-50">{pending ? "Applying…" : "Confirm prepared decision"}</button>
  </section>;
}
