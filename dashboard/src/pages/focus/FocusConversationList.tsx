import { Link } from "react-router-dom";

import {
  conversationLabel,
  lastInputAt,
  useConversationEnabled,
  useConversationHistory,
  type ConversationHistoryRecord,
} from "./focusConversations";
import { useFocusChrome } from "./focusChrome";
import { focusConversationHref } from "./routes";

const ROW = "block py-2 text-sm hover:bg-gray-900";
const SECTION = "rounded-lg border border-gray-800 p-3";

function lastInputLabel(conversation: ConversationHistoryRecord): string {
  const at = lastInputAt(conversation);
  return at ? new Date(at * 1000).toLocaleString() : "No inputs";
}

function Row({ conversation }: { conversation: ConversationHistoryRecord }) {
  return (
    <li>
      <Link to={focusConversationHref(conversation.id)} className={ROW}>
        <span className="block font-medium text-gray-100">{conversationLabel(conversation)}</span>
        <span className="block text-xs text-gray-500">
          {conversation.state} · last message {lastInputLabel(conversation)}
        </span>
      </Link>
    </li>
  );
}

/**
 * `/focus/conversations` — the supervisor's conversations as a phone list.
 * The detail page is the address a truncated chat reply links to; this is how
 * a phone reaches it without the desktop's select box.
 */
export default function FocusConversationList() {
  useFocusChrome({ title: "Conversations", fullHref: "/conversations" });
  const enabled = useConversationEnabled();
  const history = useConversationHistory(null);

  if (enabled.data && enabled.data.enabled !== true) {
    return <p className="p-4 text-sm text-gray-400">Discord conversations are disabled.</p>;
  }
  if (history.isError) {
    return (
      <p role="alert" className="p-4 text-sm text-red-300">
        {history.error instanceof Error ? history.error.message : "Conversations could not be read."}
      </p>
    );
  }

  const conversations = history.data?.conversations ?? [];
  return (
    <div className="space-y-4 p-3">
      <section className={SECTION}>
        <h2 className="text-sm font-semibold text-gray-200">Conversations · {conversations.length}</h2>
        {history.isLoading && <p className="pt-2 text-sm text-gray-500">Loading…</p>}
        {!history.isLoading && conversations.length === 0 && (
          <p className="pt-2 text-sm text-gray-500">No conversations yet.</p>
        )}
        <ul className="divide-y divide-gray-800">
          {conversations.map((conversation) => (
            <Row key={conversation.id} conversation={conversation} />
          ))}
        </ul>
      </section>
      {history.data?.next_before != null && (
        <p className="text-xs text-gray-500">Older conversations are not shown on this page.</p>
      )}
    </div>
  );
}