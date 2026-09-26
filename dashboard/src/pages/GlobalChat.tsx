import ChatConversation from "./chat/ChatConversation";
import { useSearchParams } from "react-router-dom";
import ConversationPicker from "./chat/ConversationPicker";
import { useQuery } from "@tanstack/react-query";
import { supervisorInboxStatus } from "../api/client";

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
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
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
          <ChatConversation
            key={threadId}
            projectId=""
            sessionAddress="supervisor-global"
            threadIdOverride={threadId}
            headerText="Agent Q"
          />
        </>
      )}
    </div>
  );
}
