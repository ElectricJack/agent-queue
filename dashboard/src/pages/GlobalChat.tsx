import ChatConversation from "./chat/ChatConversation";

/**
 * `/` in v2 — the global Agent Q supervisor chat. Uses the supervisor-global
 * session cold-started by the daemon (loopback-restricted, admin-scoped).
 */
export default function GlobalChat() {
  return (
    <ChatConversation
      projectId=""
      sessionAddress="supervisor-global"
      threadIdOverride="dashboard:global"
      headerText="Agent Q"
    />
  );
}
