import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useEscalation, useEscalationReply } from "../../api/messaging";
import { choiceLabels, when } from "./focusFormat";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { focusTaskHref } from "./routes";

/** `OPEN_ESCALATION_STATES` in `escalation_queries.py`; terminal is the rest. */
const OPEN_STATES = new Set(["needs_human", "reply_received", "resolving"]);

const LABEL = "block text-sm text-gray-400";
const CONTROL = "rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100";
const BUTTON = "inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium";

/** A stable per-reply identity, so a double tap is idempotent server-side. */
function replyMessageId(escalationId: string): string {
  return `dashboard:${escalationId}:${Date.now()}`;
}

/** `/focus/escalations/:escalationId` — the decision, its context, and the answer box. */
export default function FocusEscalation() {
  const { escalationId = "" } = useParams();
  return <FocusEscalationContent key={escalationId} escalationId={escalationId} />;
}

function FocusEscalationContent({ escalationId }: { escalationId: string }) {
  const query = useEscalation(escalationId || null);
  const reply = useEscalationReply();
  const [text, setText] = useState("");
  const [replyError, setReplyError] = useState<string | null>(null);
  const escalation = query.data?.escalation;
  const title = escalation?.decision_requested || "Escalation";
  // No full-dashboard counterpart: the focus page is the escalation's one
  // address (spec §6.1), which is why no link ever points at /settings/messaging.
  useFocusChrome({ title });

  if (query.isError) {
    return (
      <FocusError
        title="Escalation"
        message={query.error instanceof Error ? query.error.message : "The escalation could not be read."}
        onRetry={() => void query.refetch()}
      />
    );
  }
  if (!escalation) {
    return <p className="p-4 text-sm text-gray-500">Loading…</p>;
  }

  const choices = choiceLabels(escalation.choices);
  const open = OPEN_STATES.has(escalation.state);
  const send = async (body: string) => {
    setReplyError(null);
    try {
      await reply.mutateAsync({
        escalation_id: escalation.id,
        text: body,
        external_message_id: replyMessageId(escalation.id),
      });
      setText("");
    } catch (error) {
      setReplyError(error instanceof Error ? error.message : "The reply was not accepted.");
    }
  };

  return (
    <article className="space-y-4 p-3">
      <header className="space-y-1">
        <p className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
          <span className="rounded bg-gray-800 px-1.5 py-0.5 font-mono">{escalation.state}</span>
          <span className="rounded bg-gray-800 px-1.5 py-0.5">{escalation.severity}</span>
          <span>{escalation.project_id}</span>
          <span>opened {when(escalation.created_at)}</span>
        </p>
        <h2 className="text-lg font-semibold">{escalation.decision_requested}</h2>
        <p className="text-sm text-gray-300">{escalation.summary}</p>
        {escalation.investigation && (
          <p className="text-sm text-gray-400">Already tried: {escalation.investigation}</p>
        )}
      </header>

      {escalation.task_id && (
        <Link data-primary-control to={focusTaskHref(escalation.task_id)} className="inline-flex items-center gap-1 text-sm text-indigo-300">
          Task {escalation.task_title || escalation.task_id}
        </Link>
      )}

      {escalation.state === "needs_human" && !choices.length && (
        <p className="text-sm text-gray-400">Answer below; the supervisor acts on it.</p>
      )}

      {open && choices.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-gray-300">Options</h3>
          <div className="grid gap-2">
            {choices.map((choice) => (
              <button
                key={choice}
                type="button"
                data-primary-control
                disabled={reply.isPending}
                onClick={() => void send(choice)}
                className={`${BUTTON} border border-gray-700 text-left text-gray-100 disabled:opacity-50`}
              >
                {choice}
              </button>
            ))}
          </div>
        </section>
      )}

      {!open && (
        <p className="rounded-md border border-gray-800 bg-gray-900/50 p-3 text-sm text-gray-300">
          {escalation.state === "resolved" ? "Resolved" : escalation.state === "cancelled" ? "Cancelled" : "Stale"}
          {escalation.terminal_outcome ? `: ${escalation.terminal_outcome}` : ""}. No reply is needed.
        </p>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-300">Thread</h3>
        {(query.data?.messages ?? []).length === 0 && (
          <p className="text-sm text-gray-500">No replies yet.</p>
        )}
        <ol className="space-y-2">
          {(query.data?.messages ?? []).map((message) => (
            <li key={message.id} className="rounded-md border border-gray-800 p-2 text-sm">
              <p className="text-xs text-gray-500">
                {message.direction} · {message.verified_actor} · {when(message.received_at)}
              </p>
              <p className="whitespace-pre-wrap text-gray-100">{message.text}</p>
            </li>
          ))}
        </ol>
      </section>

      {open && (
        <section className="space-y-2">
          <label className={LABEL} htmlFor={`escalation-reply-${escalation.id}`}>Reply</label>
          <textarea
            id={`escalation-reply-${escalation.id}`}
            rows={3}
            value={text}
            onChange={(event) => setText(event.target.value)}
            className={`${CONTROL} w-full`}
          />
          {replyError && <p role="alert" className="text-sm text-red-300">{replyError}</p>}
          <button
            type="button"
            data-primary-control
            disabled={!text.trim() || reply.isPending}
            onClick={() => void send(text.trim())}
            className={`${BUTTON} bg-indigo-600 text-white disabled:opacity-50`}
          >
            Send to supervisor
          </button>
          <p className="text-xs text-gray-500">
            Resolving is the supervisor&apos;s action; this page answers the decision.
          </p>
        </section>
      )}
    </article>
  );
}