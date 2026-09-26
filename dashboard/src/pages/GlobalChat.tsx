import ChatConversation from "./chat/ChatConversation";
import { useSearchParams } from "react-router-dom";
import ConversationPicker from "./chat/ConversationPicker";

/**
 * Global Agent Q supervisor conversation view. Uses the supervisor-global
 * session cold-started by the daemon (admin-scoped).
 */
export default function GlobalChat() {
  const [searchParams, setSearchParams] = useSearchParams();
  const conversationId = searchParams.get("conversation");
  const threadId = conversationId ? `conversation:${conversationId}` : "dashboard:global";
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
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
    </div>
  );
}
