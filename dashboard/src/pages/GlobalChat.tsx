import ChatConversation from "./chat/ChatConversation";
import { useSearchParams } from "react-router-dom";
import ConversationPicker from "./chat/ConversationPicker";
import { useQuery } from "@tanstack/react-query";
import { supervisorInboxStatus, supervisorInboxHistory } from "../api/client";

/**
 * Global Agent Q supervisor conversation view. Uses the supervisor-global
 * session cold-started by the daemon (admin-scoped).
 */
export default function GlobalChat() {
  const [searchParams, setSearchParams] = useSearchParams();
  const conversationId = searchParams.get("conversation");
  const threadId = conversationId ? `conversation:${conversationId}` : "dashboard:global";
  const status = useQuery({
    queryKey: ["supervisor-inbox", "status"],
    queryFn: async () => {
      const { data } = await supervisorInboxStatus({ body: {} });
      if (!data || !data.success || !("enabled" in data)) {
        throw new Error(data && "error" in data && typeof data.error === "string"
          ? data.error : "Failed to load conversation status");
      }
      return data;
    },
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 1,
  });
  return (
    <div className="dashboard-page flex h-full min-h-0 min-w-0 flex-col gap-3 p-4">
      <h1 className="text-lg font-semibold">Supervisor conversations</h1>
      {status.isLoading ? (
        <p className="text-sm text-gray-500">Loading conversation status…</p>
      ) : status.error ? (
        <p role="alert" className="text-sm text-red-400">{status.error.message}</p>
      ) : status.data?.enabled !== true ? (
        <p className="text-sm text-gray-500">Discord conversations are disabled.</p>
      ) : (
        <>
          <ConversationPicker threadId={threadId} onSelect={(thread) => {
            setSearchParams((current) => {
              const next = new URLSearchParams(current);
              if (thread === "dashboard:global") next.delete("conversation");
              else next.set("conversation", thread.slice("conversation:".length));
              return next;
            });
          }} />
          {conversationId ? <SelectedConversation key={threadId} id={conversationId} /> :
            <ChatConversation key={threadId} projectId="" sessionAddress="supervisor-global"
              threadIdOverride={threadId} headerText="Agent Q" />}
        </>
      )}
    </div>
  );
}


/** Validate a cold link independently of the picker's paginated recent list. */
function SelectedConversation({ id }: { id: string }) {
  const history = useQuery({
    queryKey: ["supervisor-inbox", "conversation", id],
    queryFn: async () => {
      const { data } = await supervisorInboxHistory({ body: { conversation_id: id, limit: 50 } });
      if (!data || !data.success || !("conversations" in data)) {
        throw new Error(data && "error" in data && typeof data.error === "string" ? data.error : "Failed to load conversation");
      }
      return data;
    },
    refetchInterval: 30_000,
    retry: 1,
  });
  if (history.isPending) return <p>Loading conversation…</p>;
  if (history.isError) return <div role="alert">{history.error.message}
    <button className="ml-2 rounded border border-gray-700 px-3 py-2" onClick={() => void history.refetch()}>Retry conversation</button>
  </div>;
  const conversation = history.data.conversations.find((row) => row.id === id);
  if (!conversation) return <p role="status">No conversation with that id. Choose another conversation above.</p>;
  const lastInput = Math.max(0, ...conversation.inputs.map((input) => input.received_at));
  return <>
    <header className="space-y-1 text-xs text-gray-400">
      <p className="font-mono">{conversation.id}</p>
      <p>{conversation.created_by} · {conversation.transport} · {conversation.state}</p>
      <p>{lastInput ? `Last message ${new Date(lastInput * 1000).toLocaleString()}` : "No inputs"}</p>
    </header>
    <ChatConversation projectId="" sessionAddress="supervisor-global"
      threadIdOverride={`conversation:${id}`} headerText="Agent Q" />
  </>;
}
