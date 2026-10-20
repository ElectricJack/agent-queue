import { useState } from "react";
import { Link } from "react-router-dom";
import { useAsk } from "../../api/asks";
import { ReviewPane } from "../../panes/review";
import { DiscussionPanel } from "../../panes/review/DiscussionPanel";
import { AskDecision } from "./AskDecision";

export function AskDetail({ askId }: { askId: string }) {
  const query = useAsk(askId);
  const [discussionDraft, setDiscussionDraft] = useState(false);
  if (query.isError && !query.data) return <div role="alert" className="p-3">{query.error.message}
    <button type="button" className="min-h-11 px-3" onClick={() => void query.refetch()}>Retry</button></div>;
  if (!query.data) return <p role="status" className="p-3">Loading request…</p>;
  const data = query.isError ? { ...query.data, actions: query.data.actions.map(action =>
    action.action === "decide" || action.action === "prepare_decision"
      ? { ...action, enabled: false, disabled_reason: "Refresh request context before deciding" } : action) } : query.data;
  const ask = data.ask;
  return <article className="space-y-3 p-3">
    {query.isError && <p role="alert">Request refresh failed. Your draft is preserved.
      <button className="min-h-11 px-3" onClick={() => void query.refetch()}>Retry</button></p>}
    <header><h1 className="text-xl font-semibold">{ask.context.title}</h1>
      <p>{ask.context.why}</p><p className="text-sm text-gray-400">{ask.project_id} · {ask.severity} · generation {ask.decision_generation} · feedback {ask.feedback_version}</p>
      <p role="status">{ask.application_state === "not_started" ? ask.decision_state : ask.application_state}</p>
      {ask.held_while_paused && <p>Held while paused</p>}
      {data.source_health !== "healthy" && <p role="status">Request is {data.source_health}; refresh before deciding.</p>}
      <p className="text-sm">Requested by {ask.context.requested_by?.label} · {ask.context.requested_by?.origin}</p>
      <ul>{ask.context.links?.map(link => <li key={link.url}><a className="inline-block min-h-11 py-2 text-indigo-200" href={link.url}>{link.label}</a></li>)}</ul>
    </header>
    <ul>{ask.context.blocked_work?.map(link => <li key={link.url}><Link className="inline-block min-h-11 py-2 text-indigo-200" to={link.url}>{link.label}</Link></li>)}</ul>
    {ask.source_kind === "review" ? <div className="min-h-[36rem]"><ReviewPane reviewId={ask.source_key} askData={data} /></div> : <>
      <DiscussionPanel data={data} onDraftChange={setDiscussionDraft} />
      <AskDecision data={data} draftBlocked={discussionDraft} />
    </>}
    <section aria-label="Decision receipt" className="space-y-2 border-t border-gray-800 pt-3">
      <h2 className="font-semibold">Decision and application receipt</h2>
      {data.decision ? <><p>{String(data.decision.id)} · {String(data.decision.option_key)}</p>
        <p>Recorded by {String(data.decision.actor_id)}</p></> : <p>No decision recorded.</p>}
      {(data.operations ?? []).map(operation => <div key={String(operation.id)}>
        <p>{String(operation.operation_kind ?? operation.action)} · {String(operation.status)}</p>
        <pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(operation.receipt ?? operation.result, null, 2)}</pre>
      </div>)}
    </section>
    <section aria-label="Follow-up obligations"><h2 className="font-semibold">Follow-up obligations ({data.obligations?.length ?? 0})</h2>
      <p className="text-sm text-gray-400">These remain separate from pending decisions.</p>
      <ul>{data.obligations?.map(item => <li key={String(item.id)} className="mt-2">
        <p className="whitespace-pre-wrap">{String(item.original_text)} — {String(item.progress)}</p>
        {item.task_id != null && <Link className="inline-block min-h-11 py-2 text-indigo-200" to={`/focus/tasks/${encodeURIComponent(String(item.task_id))}`}>Assigned work {String(item.task_id)}</Link>}
        <p className="text-xs">{String(item.logical_owner ?? "")}</p>
      </li>)}</ul>
    </section>
  </article>;
}
