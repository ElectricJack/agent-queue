import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useChatTranscript } from "./useChatTranscript";
import InlineEventCard from "./InlineEventCard";
import ThinkingBubble from "./ThinkingBubble";
import type { PendingMessage } from "./useChatTranscript";

function fmt(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

function Bubble({ msg }: { msg: PendingMessage }) {
  const mine = msg.from_kind === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          mine
            ? msg.failed
              ? "bg-red-600/20 text-red-100"
              : "bg-indigo-600/20 text-indigo-100"
            : "bg-gray-800 text-gray-100"
        } ${msg.pending ? "opacity-60" : ""}`}
      >
        <div className="mb-1 flex items-center gap-2 text-xs text-gray-400">
          <span className="font-mono">{`${msg.from_kind}:${msg.from_id}`}</span>
          <span>{fmt(msg.created_at)}</span>
          {msg.pending && <span className="text-gray-500">sending…</span>}
          {msg.failed && <span className="text-red-400">failed</span>}
        </div>
        <div className="whitespace-pre-wrap">{msg.body}</div>
      </div>
    </div>
  );
}

interface Props {
  projectId?: string;
  sessionAddress?: string;
  threadIdOverride?: string;
  headerText?: string;
}

export default function ChatConversation(props: Props = {}) {
  const params = useParams();
  const projectId = props.projectId ?? params.projectId ?? "";
  const { items, isLoading, error, send, isSending, sendError, thinking } =
    useChatTranscript(projectId, {
      sessionAddress: props.sessionAddress,
      threadIdOverride: props.threadIdOverride,
    });
  const [body, setBody] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [items, thinking]);

  const submit = () => {
    if (!body.trim() || isSending) return;
    void send(body).then(() => setBody(""));
  };

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col space-y-3 md:h-[calc(100vh-4rem)]">
      <header className="hidden md:block">
        <h2 className="text-lg font-semibold">
          {props.headerText ?? "Chat with supervisor"}
        </h2>
        <p className="text-xs text-gray-500">
          Talking to{" "}
          <span className="font-mono">
            {props.sessionAddress ?? `supervisor-${projectId}`}
          </span>
          .
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
        {!isLoading && items.length === 0 && (
          <p className="text-sm text-gray-500">No messages yet — say hello.</p>
        )}
        {items.map((it, idx) =>
          it.kind === "message" ? (
            <Bubble key={`m-${it.msg.id}-${idx}`} msg={it.msg} />
          ) : (
            <InlineEventCard key={`e-${idx}`} event={it.event} ts={it.ts} />
          ),
        )}
        {thinking && <ThinkingBubble thinking={thinking} />}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            // Enter submits, Shift-Enter newline, $mod-Enter also submits.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          placeholder="Message the supervisor (Enter to send, Shift-Enter for newline)…"
          className="flex-1 resize-none rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        />
        <button
          type="submit"
          disabled={isSending || !body.trim()}
          className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          <PaperAirplaneIcon className="h-4 w-4" />
          <span className="hidden sm:inline">Send</span>
        </button>
      </form>
      {sendError !== null && (
        <p className="text-xs text-red-400">{(sendError as Error).message}</p>
      )}
    </div>
  );
}
