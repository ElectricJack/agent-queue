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

interface EventStreamContextValue {
  status: ConnectionStatus;
  events: EventEntry[];
  clearEvents: () => void;
  onTaskMessage: (handler: (event: TaskMessageEvent) => void) => () => void;
}

const EventStreamContext = createContext<EventStreamContextValue>({
  status: "disconnected",
  events: [],
  clearEvents: () => {},
  onTaskMessage: () => () => {},
});

export function useEventStreamStatus(): ConnectionStatus {
  return useContext(EventStreamContext).status;
}

export function useEventBuffer() {
  const ctx = useContext(EventStreamContext);
  return { events: ctx.events, clearEvents: ctx.clearEvents };
}

export function useTaskMessageSubscription() {
  return useContext(EventStreamContext).onTaskMessage;
}

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [events, setEvents] = useState<EventEntry[]>([]);
  const nextId = useRef(0);
  const [taskMessageListeners] = useState(
    () => new Set<(event: TaskMessageEvent) => void>(),
  );

  const addEvent = useCallback((event: NotifyEvent) => {
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

  return (
    <EventStreamContext.Provider value={{ status, events, clearEvents, onTaskMessage }}>
      {children}
    </EventStreamContext.Provider>
  );
}
