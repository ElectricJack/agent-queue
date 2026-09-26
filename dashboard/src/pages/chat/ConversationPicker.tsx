import { useInfiniteQuery } from "@tanstack/react-query";
import { supervisorInboxHistory, type ConversationHistoryRecord } from "../../api/client";

/** `(before, before_id)` pages rows that share a timestamp without skipping any. */
type HistoryCursor = { before: number; before_id: string | null };

function label(conversation: ConversationHistoryRecord): string {
  const creator = conversation.created_by.startsWith("human:discord:")
    ? `Discord · ${conversation.created_by.slice("human:discord:".length)}`
    : conversation.created_by;
  const lastInput = Math.max(...conversation.inputs.map((input) => input.received_at));
  const lastInputLabel = Number.isFinite(lastInput)
    ? new Date(lastInput * 1000).toLocaleString()
    : "No inputs";
  return `${creator} · ${conversation.state} · ${lastInputLabel} · ${conversation.id}`;
}

export default function ConversationPicker({ threadId, onSelect }: {
  threadId: string;
  onSelect: (threadId: string) => void;
}) {
  const history = useInfiniteQuery({
    queryKey: ["supervisor-inbox", "history"],
    initialPageParam: undefined as HistoryCursor | undefined,
    queryFn: async ({ pageParam }) => {
      const { data } = await supervisorInboxHistory({
        body: {
          limit: 50,
          ...(pageParam === undefined ? {} : { before: pageParam.before }),
          ...(pageParam?.before_id == null ? {} : { before_id: pageParam.before_id }),
        },
      });
      if (!data || !data.success || !("conversations" in data)) {
        throw new Error(data && "error" in data && typeof data.error === "string" ? data.error : "Failed to load conversations");
      }
      return data;
    },
    getNextPageParam: (page): HistoryCursor | undefined => page.next_before == null
      ? undefined
      : { before: page.next_before, before_id: page.next_before_id },
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 1,
  });
  const conversations = [...new Map(
    (history.data?.pages.flatMap((page) => page.conversations) ?? [])
      .map((conversation) => [conversation.id, conversation]),
  ).values()];

  return (
    <div className="space-y-1 text-xs">
      <label className="flex flex-wrap items-center gap-2">
        <span className="text-gray-400">Conversation</span>
        <select
          aria-label="Conversation"
          value={threadId}
          onChange={(event) => onSelect(event.target.value)}
          className="min-w-0 flex-1 rounded border border-gray-800 bg-gray-900 px-2 py-1 text-gray-200"
        >
          <option value="dashboard:global">All</option>
          {threadId !== "dashboard:global" && !conversations.some((row) => row.thread_id === threadId) && (
            <option value={threadId}>{threadId}</option>
          )}
          {conversations.map((conversation) => (
            <option key={conversation.id} value={conversation.thread_id}>{label(conversation)}</option>
          ))}
        </select>
      </label>
      {history.isLoading && <p className="text-gray-500">Loading conversations…</p>}
      {history.error && <p role="alert" className="text-red-400">{history.error.message}</p>}
      {history.hasNextPage && (
        <button type="button" disabled={history.isFetchingNextPage}
          onClick={() => void history.fetchNextPage()}
          className="text-indigo-300 disabled:opacity-50">
          Load older conversations
        </button>
      )}
    </div>
  );
}
