import { useEffect, useRef, useState, type RefObject } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { askKeys, useDecideAsk, usePrepareAsk, type AskDetailData } from "../../api/asks";
import type { AskPrepareDecisionRequest, AskPreview } from "../../api/client";
import { useDraftWarning } from "./useDraftWarning";
import { DecisionPreview } from "../../panes/review/DecisionPreview";
import { useRawEventSubscription } from "../../ws/useEventStream";

export function AskDecision({ data, draftBlocked = false, saveDraft, pendingSave = false, approveRef, responseRoute }: {
  approveRef?: RefObject<HTMLButtonElement | null>;
  responseRoute?: { summary: string; class_summaries: Record<string, string> };
  data: AskDetailData; draftBlocked?: boolean; saveDraft?: () => Promise<void>; pendingSave?: boolean;
}) {
  const prepare = usePrepareAsk();
  const decide = useDecideAsk();
  const client = useQueryClient();
  const [responderClass, setResponderClass] = useState("");
  const [note, setNote] = useState("");
  const [prepared, setPrepared] = useState<{ preview: AskPreview; args: AskPrepareDecisionRequest; key: string } | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const epoch = useRef(0);
  const version = JSON.stringify(data.expected);
  useEffect(() => {
    if (prepared && JSON.stringify(prepared.args.expected) !== version) setStale(true);
  }, [version, prepared]);
  useDraftWarning(Boolean(note));
  useRawEventSubscription(event => {
    if (/^(ask|review)\./.test(event.event_type)) {
      epoch.current++; setStale(true);
      void client.invalidateQueries({ queryKey: askKeys.all });
    }
  });
  useEffect(() => {
    if (!prepared) return;
    const timer = window.setTimeout(() => setStale(true), Math.max(0, prepared.preview.expires_at * 1000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [prepared]);
  const pending = saving || pendingSave || prepare.isPending || decide.isPending;
  const build = async (option: string, save = false) => {
    setError(null); setPrepared(null);
    try {
      let expected = data.expected;
      if (save && saveDraft) {
        setSaving(true);
        await saveDraft();
        // All comment acknowledgements precede this fresh expected version.
        await client.invalidateQueries({ queryKey: askKeys.detail(data.ask.id) });
        const result = await client.fetchQuery({ queryKey: askKeys.detail(data.ask.id) });
        expected = (result as AskDetailData).expected;
      }
      const args = { ask_id: data.ask.id, expected, option_key: option, overall_note: note, ...(option === "request_changes" && responderClass ? { responder_class: responderClass } : {}) };
      const started = epoch.current;
      const result = await prepare.mutateAsync(args);
      setPrepared({ preview: result.preview, args, key: crypto.randomUUID() });
      setStale(started !== epoch.current);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Preparation failed; your draft is preserved."); }
    finally { setSaving(false); }
  };
  const confirm = async () => {
    if (!prepared || stale || draftBlocked || pending) return;
    setError(null);
    try {
      await decide.mutateAsync({ ...prepared.args, preview_id: prepared.preview.id, idempotency_key: prepared.key });
      setPrepared(null); setNote("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Decision failed. Refresh to inspect its receipt.");
      setStale(true);
    }
  };
  const choices = data.actions.filter(a => a.action === "decide");
  return <section aria-label="Decision" className="space-y-3 border-t border-gray-800 p-3">
    <p className="text-sm">{data.ask.context.waiting_effect}</p>
    {responseRoute && <>
      <p aria-label="Response route" className="text-sm">{responseRoute.class_summaries[responderClass] ?? responseRoute.summary}</p>
      <label className="block text-sm">Revision intelligence class
        <select value={responderClass} disabled={pending} onChange={e => { setResponderClass(e.target.value); setStale(true); }}
          className="ml-2 min-h-11 rounded border border-gray-700 bg-gray-900 px-2">
          <option value="">Project default</option>
          {Object.keys(responseRoute.class_summaries).map(value => <option key={value}>{value}</option>)}
        </select>
      </label>
    </>}
    <label className="block text-sm">Overall decision note
      <textarea value={note} onChange={e => { setNote(e.target.value); setStale(true); }} disabled={pending}
        className="mt-1 block w-full rounded border border-gray-700 bg-gray-900 p-2" />
    </label>
    {note && <div><p role="status" className="text-sm">Decision note draft — recorded when you confirm the prepared decision.</p>
      <button type="button" disabled={pending} className="min-h-11 px-3"
        onClick={() => { setNote(""); setStale(true); }}>Discard decision note</button></div>}
    {draftBlocked && <p role="status">Draft — save or explicitly discard before deciding.</p>}
    {(pendingSave || saving) && <p role="status">Saving feedback… Decision is disabled until every save is acknowledged.</p>}
    <div className="flex flex-wrap gap-2">{choices.map(action => <button key={String(action.option_key)} type="button"
      title={typeof action.disabled_reason === "string" ? action.disabled_reason : undefined}
      ref={action.option_key === "approve" ? approveRef : undefined}
      disabled={!action.enabled || pending || draftBlocked || ((action.option_key === "reject" || action.option_key === "decline") && !note.trim())}
      onClick={() => void build(String(action.option_key))}
      className="min-h-11 rounded border border-gray-600 px-3 py-2 disabled:opacity-50">Prepare {String(action.label)}</button>)}</div>
    {[...new Set(choices.map(a => a.disabled_reason).filter(Boolean))].map(reason =>
      <p key={String(reason)} className="text-sm text-amber-200">{String(reason)}</p>)}
    {draftBlocked && saveDraft && <button type="button" disabled={pending || !choices.some(a => a.option_key === "approve" && a.enabled)}
      className="min-h-11 rounded bg-indigo-700 px-3 py-2 disabled:opacity-50" onClick={() => void build("approve", true)}>Save and prepare approval</button>}
    {error && <p role="alert" className="whitespace-pre-wrap text-red-300">{error}</p>}
    {prepared && <DecisionPreview preview={prepared.preview} stale={stale || draftBlocked} pending={pending} onConfirm={() => void confirm()} />}
  </section>;
}
