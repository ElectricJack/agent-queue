import { useId } from "react";
import type {
  CollaborationMemberRecord,
  CollaborationMessageRecord,
  CollaborationThreadRecord,
} from "../api/client";
import { useCollaborationThread, useTaskCollaborations } from "../api/collaboration";
import { formatClock, formatDuration } from "../pages/metrics/providerAvailabilityFormat";
import StatusBadge from "./StatusBadge";

const stateColors: Record<CollaborationThreadRecord["state"], string> = {
  active: "bg-green-500/10 text-green-400",
  closed: "bg-gray-500/10 text-gray-400",
  expired: "bg-orange-500/10 text-orange-400",
};

const words = (value: string) => value.replace(/_/g, " ");

/**
 * Read-only view of the collaboration threads a task belongs to. Agents send
 * and close through `aq collaboration` / `aq message`; nothing here writes.
 */
export default function TaskCollaboration({ taskId }: { taskId: string }) {
  const headingId = useId();
  const { data } = useTaskCollaborations(taskId);
  const threads = data?.threads ?? [];
  if (threads.length === 0) return null;
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Collaboration</h2>
      <p className="text-xs text-gray-400">Threads this task shares with other tasks. Member agents send and close them.</p>
      <ul className="space-y-3">
        {threads.map((thread) => <CollaborationThread key={thread.id} summary={thread} />)}
      </ul>
    </section>
  );
}

function CollaborationThread({ summary }: { summary: CollaborationThreadRecord }) {
  const detail = useCollaborationThread(summary.id);
  // The thread read is fetched after the list, so it is the fresher copy.
  const thread = detail.data?.thread ?? summary;
  const now = Date.now() / 1000;
  const messages = [...(detail.data?.messages ?? [])].sort((a, b) => a.seq - b.seq);
  return (
    <li className="min-w-0 space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${stateColors[thread.state]}`}>
          {thread.state}
        </span>
        <span className="min-w-0 break-all font-mono text-xs text-gray-400">{thread.id}</span>
      </div>
      {thread.goal && <p className="whitespace-pre-wrap break-words text-sm text-gray-200">{thread.goal}</p>}
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400">
        <div>
          <dt className="sr-only">Deadline</dt>
          <dd>{deadlineText(thread, now)}</dd>
        </div>
        <div>
          <dt className="sr-only">Budget</dt>
          <dd>{thread.message_count}/{thread.message_budget} messages</dd>
        </div>
      </dl>
      <ul aria-label="Members" className="space-y-1">
        {thread.members.map((member) => <MemberRow key={member.task_id} member={member} />)}
      </ul>
      {thread.state !== "active" && <FinalResult thread={thread} />}
      {detail.isPending && <p role="status" className="text-sm text-gray-400">Loading messages…</p>}
      {detail.isError && (
        <p role="alert" className="rounded border border-red-800 bg-red-950/30 p-2 text-sm text-red-300">
          Could not load this thread. {detail.error.message}
        </p>
      )}
      {detail.data && (
        messages.length === 0 ? (
          <p className="text-sm text-gray-400">No messages yet.</p>
        ) : (
          <>
            {detail.data.has_more && <p className="text-xs text-gray-500">Older messages are not shown.</p>}
            <ol aria-label="Messages" className="space-y-2">
              {messages.map((message) => <MessageRow key={message.seq} message={message} now={now} />)}
            </ol>
          </>
        )
      )}
    </li>
  );
}

function deadlineText(thread: CollaborationThreadRecord, now: number): string {
  if (thread.state === "active") {
    return thread.remaining_seconds > 0 ? `Ends in ${formatDuration(thread.remaining_seconds)}` : "Deadline passed";
  }
  return thread.closed_at != null ? `Ended ${formatClock(thread.closed_at, now)}` : "Ended";
}

function MemberRow({ member }: { member: CollaborationMemberRecord }) {
  const joined = member.state === "removed" ? "removed"
    : member.needs_accept ? (member.state === "accepted" ? "needs accept" : "invited")
      : "accepted";
  const joinedColor = joined === "accepted" ? "text-green-400"
    : joined === "removed" ? "text-gray-500" : "text-yellow-400";
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span className="min-w-0 break-all font-mono text-gray-300">{member.task_id}</span>
      <span className="inline-flex items-center gap-1 text-gray-400">
        <span aria-hidden="true" className={`h-2 w-2 rounded-full ${member.running ? "bg-green-400" : "bg-gray-600"}`} />
        {member.running ? "running" : "not running"}
      </span>
      <StatusBadge status={member.task_status} />
      <span className={joinedColor}>{joined}</span>
    </li>
  );
}

function FinalResult({ thread }: { thread: CollaborationThreadRecord }) {
  const labelId = useId();
  const result = thread.final_result ?? {};
  const reason = typeof result.reason === "string" ? result.reason : thread.close_reason ?? thread.state;
  const note = typeof result.note === "string" && result.note ? result.note : null;
  return (
    <div role="group" aria-labelledby={labelId} className="space-y-1 rounded border border-gray-800 bg-gray-950 p-2 text-sm">
      <p id={labelId} className="text-xs font-semibold uppercase text-gray-500">Final result</p>
      <p className="text-gray-300">Ended: {words(reason)}</p>
      {note && <p className="whitespace-pre-wrap break-words text-gray-200">{note}</p>}
    </div>
  );
}

function MessageRow({ message, now }: { message: CollaborationMessageRecord; now: number }) {
  const sent = new Date(message.created_at * 1000);
  return (
    <li className="rounded border border-gray-800 bg-gray-950 p-2">
      <div className="mb-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-400">
        <span>#{message.seq}</span>
        <span className="min-w-0 break-all font-mono">{message.sender_task_id}</span>
        <time dateTime={sent.toISOString()} title={sent.toLocaleString()}>{formatClock(message.created_at, now)}</time>
      </div>
      {message.subject && <div className="break-words text-sm font-medium text-gray-200">{message.subject}</div>}
      {message.body != null ? (
        <p className="whitespace-pre-wrap break-words text-sm text-gray-200">{message.body}</p>
      ) : (
        <p className="text-sm italic text-gray-500">Message content has expired.</p>
      )}
    </li>
  );
}
