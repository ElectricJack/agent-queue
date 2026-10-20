import { useRef, useState } from "react";
import { useReplyAsk, type AskDetailData } from "../../api/asks";
import { useDraftWarning } from "../../components/asks/useDraftWarning";

export function DiscussionPanel({ data, onDraftChange, onPromote }: {
  data: AskDetailData; onDraftChange?: (dirty: boolean) => void;
  onPromote?: (text: string, discussionId: string) => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const key = useRef(crypto.randomUUID());
  const reply = useReplyAsk();
  const action = data.actions.find(a => a.action === "discuss");
  useDraftWarning(Boolean(text));
  const edit = (value: string) => { setText(value); onDraftChange?.(Boolean(value)); setSaved(false); key.current = crypto.randomUUID(); };
  const send = async () => {
    setError(null);
    try {
      await reply.mutateAsync({ ask_id: data.ask.id, expected: data.expected, text,
        intent: "discuss", idempotency_key: key.current, post_decision: Boolean(action?.post_decision) });
      edit(""); setSaved(true);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Discussion was not saved. Your draft is preserved."); }
  };
  const revision = data.document?.revision;
  return <section aria-label="Discuss" className="space-y-3 border-t border-gray-800 p-3">
    <h3 className="font-semibold">Discuss{revision != null ? ` · revision ${String(revision)}` : ""}</h3>
    {Boolean(action?.post_decision) && <p className="text-sm text-amber-200">Post-decision discussion — the recorded decision remains unchanged.</p>}
    <p className="text-sm text-gray-400">Discussion asks the supervisor a question. Save proposed changes under Comments for revision.</p>
    <ol className="space-y-3">{(data.history ?? []).filter(e => e.entry_kind === "discussion").map(entry =>
      <li key={String(entry.id)} className="border-l border-gray-700 pl-3">
        <p className="text-xs text-gray-400">{String(entry.verified_actor ?? entry.direction)} · revision {String((entry.source_version as { revision?: number })?.revision ?? "current")}</p>
        <p className="whitespace-pre-wrap break-words">{String(entry.text)}</p>
        {entry.direction === "outbound" && onPromote && <button type="button" className="min-h-11 text-indigo-200"
          onClick={() => onPromote(String(entry.text), String(entry.id))}>Edit as formal feedback</button>}
      </li>)}</ol>
    <ul aria-live="polite" className="text-sm text-gray-400">{data.inputs.map(input => {
      const status = input.status as { state?: string } | undefined;
      return <li key={String(input.id)}>Answer {String(status?.state ?? input.state)}{input.source_version ? ` · revision ${String((input.source_version as { revision?: number }).revision ?? "current")}` : ""}</li>;
    })}</ul>
    {action?.disabled_reason != null && <p className="text-sm text-amber-200">{String(action.disabled_reason)}</p>}
    <label className="block text-sm">Discussion question
      <textarea value={text} onChange={e => edit(e.target.value)} disabled={reply.isPending}
        className="mt-1 block w-full rounded border border-gray-700 bg-gray-900 p-2" />
    </label>
    <p role="status" className="text-sm">{reply.isPending ? "Saving discussion…" : text ? "Draft — not saved" : saved ? "Saved; awaiting the supervisor answer." : ""}</p>
    {error && <p role="alert" className="text-red-300">{error}</p>}
    <div className="flex gap-2">
      <button type="button" disabled={!text.trim() || reply.isPending || !action?.enabled}
        onClick={() => void send()} className="min-h-11 rounded bg-indigo-700 px-3 py-2 disabled:opacity-50">Save discussion</button>
      <button type="button" disabled={!text || reply.isPending} onClick={() => edit("")}
        className="min-h-11 rounded border border-gray-700 px-3 py-2 disabled:opacity-50">Discard draft</button>
    </div>
  </section>;
}
