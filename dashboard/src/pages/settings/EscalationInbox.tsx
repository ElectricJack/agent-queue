import { useState } from "react";
import { Link } from "react-router-dom";

import { useEscalation, useEscalationReply, useEscalations } from "../../api/messaging";

const OPEN_STATES = new Set(["needs_human", "reply_received", "resolving"]);

function formatTime(epoch: number | null | undefined): string {
  return epoch ? new Date(epoch * 1000).toLocaleString() : "—";
}

/** A stable per-reply identity so a double submit is idempotent server-side. */
function replyMessageId(escalationId: string): string {
  return `dashboard:${escalationId}:${Date.now()}`;
}

/**
 * Scoped escalation list, conversation detail and human reply.
 *
 * The reply goes through `escalation_reply` — the same core command a Discord
 * thread reply uses — so a human answer reaches the owning supervisor by one
 * path regardless of where it was typed. Nothing here mutates a task, resolves
 * a gate or nudges a worker; those stay separate explicit dashboard actions.
 */
export default function EscalationInbox() {
  const { data, isLoading, error } = useEscalations();
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useEscalation(selected);
  const reply = useEscalationReply();
  const [text, setText] = useState("");
  const [replyError, setReplyError] = useState<string | null>(null);

  const escalations = data?.escalations ?? [];
  const open = escalations.filter((item) => OPEN_STATES.has(item.state));

  const send = async () => {
    if (!selected || !text.trim()) return;
    setReplyError(null);
    try {
      await reply.mutateAsync({
        escalation_id: selected,
        text: text.trim(),
        external_message_id: replyMessageId(selected),
      });
      setText("");
    } catch (e) {
      setReplyError((e as Error).message);
    }
  };

  return (
    <section className="space-y-3 rounded-lg border border-gray-800 p-4">
      <header>
        <h3 className="font-semibold">Escalation inbox</h3>
        <p className="text-xs text-gray-500">
          {open.length} open of {escalations.length}. A reply is persisted and handed to the owning
          supervisor, which decides and performs any recovery.
        </p>
      </header>

      {isLoading && <p className="text-sm text-gray-400">Loading escalations…</p>}
      {error && <p role="alert" className="text-sm text-red-300">{(error as Error).message}</p>}
      {!isLoading && escalations.length === 0 && (
        <p className="text-sm text-gray-400">No escalations.</p>
      )}

      <ul className="divide-y divide-gray-800">
        {escalations.map((item) => (
          <li key={item.id} className="py-2">
            <button
              type="button"
              className="w-full text-left"
              aria-expanded={selected === item.id}
              onClick={() => setSelected(selected === item.id ? null : item.id)}
            >
              <span className="flex flex-wrap items-center gap-2 text-sm">
                <span className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-xs">{item.state}</span>
                <span className="font-medium">{item.summary}</span>
                <span className="text-xs text-gray-500">{item.project_id}</span>
                {item.pending_delivery && (
                  <span className="rounded bg-amber-900/40 px-1.5 py-0.5 text-xs text-amber-200">
                    delivery pending
                  </span>
                )}
              </span>
            </button>

            {selected === item.id && (
              <div className="mt-3 space-y-3 rounded-md border border-gray-800 bg-gray-900/40 p-3 text-sm">
                <dl className="grid gap-x-6 gap-y-1 md:grid-cols-2">
                  <div className="flex justify-between gap-4"><dt className="text-gray-400">Supervisor</dt><dd className="font-mono">{item.supervisor_owner}</dd></div>
                  <div className="flex justify-between gap-4"><dt className="text-gray-400">Severity</dt><dd>{item.severity}</dd></div>
                  <div className="flex justify-between gap-4"><dt className="text-gray-400">Updated</dt><dd>{formatTime(item.updated_at)}</dd></div>
                  <div className="flex justify-between gap-4">
                    <dt className="text-gray-400">Task</dt>
                    <dd>
                      {item.task_id ? (
                        <Link className="text-indigo-400 hover:underline" to={`/tasks/${encodeURIComponent(item.task_id)}`}>
                          {item.task_id}
                        </Link>
                      ) : "—"}
                    </dd>
                  </div>
                </dl>
                <p><span className="text-gray-400">Decision requested: </span>{item.decision_requested}</p>
                <p className="text-gray-300">{item.investigation}</p>

                {(detail.data?.deliveries ?? []).some((d) => ["pending", "sending", "retry", "unknown"].includes(d.status)) && (
                  <p className="text-amber-300">
                    External delivery not confirmed:{" "}
                    {(detail.data?.deliveries ?? []).map((d) => `${d.kind} ${d.status}`).join(", ")}
                  </p>
                )}

                <ol className="space-y-1">
                  {(detail.data?.messages ?? []).map((message) => (
                    <li key={message.id} className="rounded bg-gray-900 px-2 py-1">
                      <span className="mr-2 font-mono text-xs text-gray-500">
                        {message.direction} · {message.verified_actor}
                      </span>
                      {message.text}
                    </li>
                  ))}
                </ol>

                <div className="space-y-2">
                  <label className="block" htmlFor={`escalation-reply-${item.id}`}>
                    <span className="text-gray-400">Reply</span>
                    <textarea
                      id={`escalation-reply-${item.id}`}
                      className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1"
                      rows={3}
                      value={text}
                      onChange={(e) => setText(e.target.value)}
                    />
                  </label>
                  {replyError && <p role="alert" className="text-sm text-red-300">{replyError}</p>}
                  <button
                    type="button"
                    className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                    disabled={!text.trim() || reply.isPending}
                    onClick={send}
                  >
                    Send to supervisor
                  </button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
