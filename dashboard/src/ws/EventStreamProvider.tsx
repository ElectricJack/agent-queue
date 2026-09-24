/**
 * Context provider that manages the WebSocket event stream lifecycle.
 *
 * - Collects ALL notify.* events into a persistent buffer (survives navigation)
 * - Dispatches events to TanStack Query cache for real-time updates
 * - Exposes connection status, event buffer, and task message subscriptions
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useEventStream, type ConnectionStatus } from "./useEventStream";
import type { NotifyEvent, TaskMessageEvent } from "./types";

const MAX_EVENTS = 500;

export interface EventEntry {
  id: number;
  timestamp: Date;
  event: NotifyEvent;
}

interface EventBufferValue {
  events: EventEntry[];
  clearEvents: () => void;
}

// Three contexts, because they change at very different rates: the buffer on
// every frame, the status on (re)connect, the subscription never. One shared
// value re-rendered every status reader (the Metrics page) on every frame.
const StatusContext = createContext<ConnectionStatus>("disconnected");
const BufferContext = createContext<EventBufferValue>({ events: [], clearEvents: () => {} });
const TaskMessageContext = createContext<
  (handler: (event: TaskMessageEvent) => void) => () => void
>(() => () => {});

export function useEventStreamStatus(): ConnectionStatus {
  return useContext(StatusContext);
}

export function useEventBuffer() {
  return useContext(BufferContext);
}

export function useTaskMessageSubscription() {
  return useContext(TaskMessageContext);
}

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [events, setEvents] = useState<EventEntry[]>([]);
  const nextId = useRef(0);
  const [taskMessageListeners] = useState(
    () => new Set<(event: TaskMessageEvent) => void>(),
  );

  const addEvent = useCallback((event: NotifyEvent) => {
    // Metrics ticks arrive once a second and would evict the whole activity
    // buffer every eight minutes.  The Metrics page reads them off the raw
    // subscription instead.
    if (event.event_type?.startsWith("metrics.")) return;
    const entry: EventEntry = {
      id: nextId.current++,
      timestamp: new Date(),
      event,
    };
    setEvents((prev) => {
      const next = [...prev, entry];
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
    });
  }, []);

  const clearEvents = useCallback(() => setEvents([]), []);

  const handleTaskMessage = useCallback(
    (event: TaskMessageEvent) => {
      for (const listener of taskMessageListeners) {
        listener(event);
      }
    },
    [taskMessageListeners],
  );

  const onTaskMessage = useCallback(
    (handler: (event: TaskMessageEvent) => void) => {
      taskMessageListeners.add(handler);
      return () => {
        taskMessageListeners.delete(handler);
      };
    },
    [taskMessageListeners],
  );

  useEventStream({
    onTaskMessage: handleTaskMessage,
    onEvent: addEvent,
    onStatusChange: setStatus,
  });

  const buffer = useMemo(() => ({ events, clearEvents }), [events, clearEvents]);

  return (
    <StatusContext.Provider value={status}>
      <TaskMessageContext.Provider value={onTaskMessage}>
        <BufferContext.Provider value={buffer}>{children}</BufferContext.Provider>
      </TaskMessageContext.Provider>
    </StatusContext.Provider>
  );
}
