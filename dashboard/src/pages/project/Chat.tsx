import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useChatMessages, useSendChatMessage } from "../../api/hooks";
import type { MessageModel } from "../../api/client";

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

function Bubble({ msg }: { msg: MessageModel }) {
  const mine = msg.from_kind === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
          mine ? "bg-indigo-600/20 text-indigo-100" : "bg-gray-800 text-gray-100"
        }`}
      >
        <div className="mb-1 flex items-center gap-2 text-xs text-gray-400">
          <span className="font-mono">
            {(msg as { from?: string }).from ?? `${msg.from_kind}:${msg.from_id}`}
          </span>
          <span>{fmtTime(msg.created_at)}</span>
        </div>
        <div className="whitespace-pre-wrap">{msg.body}</div>
      </div>
    </div>
  );
}

export default function ProjectChat() {
  const { projectId = "" } = useParams();
  const { data, isLoading, error } = useChatMessages(projectId);
  const send = useSendChatMessage(projectId);
  const [body, setBody] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [data]);

  const messages = data?.messages ?? [];

  return (
    <div className="flex h-[calc(100vh-14rem)] flex-col space-y-3">
      <header>
        <h2 className="text-lg font-semibold">Chat with supervisor</h2>
        <p className="text-xs text-gray-500">
          Talking to <span className="font-mono">supervisor-{projectId}</span>. Messages
          appear here as the session replies.
        </p>
      </header>

      {error && (
        <p className="text-sm text-red-400">
          Failed to load chat: {(error as Error).message}
        </p>
      )}

      <div
        ref={scrollRef}
        className="flex-1 space-y-2 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-3"
      >
        {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {messages.length === 0 && !isLoading && (
          <p className="text-sm text-gray-500">No messages yet — say hello.</p>
        )}
        {messages.map((m) => (
          <Bubble key={m.id} msg={m} />
        ))}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!body.trim() || send.isPending) return;
          send.mutate(body, { onSuccess: () => setBody("") });
        }}
      >
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              if (body.trim() && !send.isPending) {
                send.mutate(body, { onSuccess: () => setBody("") });
              }
            }
          }}
          rows={2}
          placeholder="Message the supervisor (Cmd/Ctrl+Enter to send)…"
          className="flex-1 resize-none rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        />
        <button
          type="submit"
          disabled={send.isPending || !body.trim()}
          className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          <PaperAirplaneIcon className="h-4 w-4" />
          Send
        </button>
      </form>
      {send.error && (
        <p className="text-xs text-red-400">{(send.error as Error).message}</p>
      )}
    </div>
  );
}
