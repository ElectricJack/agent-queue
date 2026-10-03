import { Link, useParams } from "react-router-dom";

import ChatConversation from "../chat/ChatConversation";
import {
  conversationLabel,
  conversationThreadId,
  lastInputAt,
  useConversationEnabled,
  useConversationHistory,
} from "./focusConversations";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { FOCUS_CONVERSATIONS } from "./routes";

/**
 * `/focus/conversations/:conversationId` — one supervisor conversation on a
 * phone: who opened it, its state, and the transcript with the reply box.
 * The transcript is the shared chat component against the global supervisor
 * session, so a message typed here is the same `agent_message` the desktop
 * `/conversations` page sends.
 */
export default function FocusConversation() {
  const { conversationId = "" } = useParams();
  return <FocusConversationContent key={conversationId} conversationId={conversationId} />;
}

function FocusConversationContent({ conversationId }: { conversationId: string }) {
  const enabled = useConversationEnabled();
  const history = useConversationHistory(conversationId || null);
  const conversation = history.data?.conversations[0];
  const title = conversation ? conversationLabel(conversation) : "Conversation";
  useFocusChrome({ title, fullHref: `/conversations?conversation=${encodeURIComponent(conversationId)}` });

  if (enabled.data && enabled.data.enabled !== true) {
    return <p className="p-4 text-sm text-gray-400">Discord conversations are disabled.</p>;
  }
  if (history.isError) {
    return (
      <FocusError
        title="Conversation"
        message={history.error instanceof Error ? history.error.message : "The conversation could not be read."}
        onRetry={() => void history.refetch()}
      />
    );
  }
  if (!history.data) {
    return <p className="p-4 text-sm text-gray-500">Loading…</p>;
  }
  if (!conversation) {
    return (
      <div className="space-y-3 p-4">
        <p className="text-sm text-gray-400">No conversation with that id.</p>
        <Link data-primary-control to={FOCUS_CONVERSATIONS} className="text-sm text-indigo-300">
          All conversations
        </Link>
      </div>
    );
  }

  const lastInput = lastInputAt(conversation);
  return (
    <div className="flex min-h-full flex-col">
      <header className="space-y-1 border-b border-gray-800 p-3 text-xs text-gray-400">
        <p className="font-mono">{conversation.id}</p>
        <p>
          {conversation.transport} · {conversation.state}
        </p>
        <p>{lastInput ? `Last message ${new Date(lastInput * 1000).toLocaleString()}` : "No inputs"}</p>
      </header>
      <div className="min-h-0 flex-1">
        <ChatConversation
          projectId=""
          sessionAddress="supervisor-global"
          threadIdOverride={conversationThreadId(conversation.id)}
        />
      </div>
    </div>
  );
}