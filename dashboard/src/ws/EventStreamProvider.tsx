/**
 * Context provider that manages the WebSocket event stream lifecycle.
 *
 * Wrap the app in <EventStreamProvider> to get:
 * - Automatic WS connection with reconnect
 * - Event dispatch to TanStack Query cache
 * - Connection status exposed via useEventStreamStatus()
 * - Task message callbacks via context
 */

import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";
import { useEventStream, type ConnectionStatus } from "./useEventStream";
import type { TaskMessageEvent } from "./types";

interface EventStreamContextValue {
  status: ConnectionStatus;
  onTaskMessage: (handler: (event: TaskMessageEvent) => void) => () => void;
}

const EventStreamContext = createContext<EventStreamContextValue>({
  status: "disconnected",
  onTaskMessage: () => () => {},
});

export function useEventStreamStatus(): ConnectionStatus {
  return useContext(EventStreamContext).status;
}

/**
 * Subscribe to task_message events for a specific task (or all tasks).
 * Returns an unsubscribe function.
 */
export function useTaskMessageSubscription() {
  return useContext(EventStreamContext).onTaskMessage;
}

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [listeners] = useState(
    () => new Set<(event: TaskMessageEvent) => void>(),
  );

  const handleTaskMessage = useCallback(
    (event: TaskMessageEvent) => {
      for (const listener of listeners) {
        listener(event);
      }
    },
    [listeners],
  );

  const onTaskMessage = useCallback(
    (handler: (event: TaskMessageEvent) => void) => {
      listeners.add(handler);
      return () => {
        listeners.delete(handler);
      };
    },
    [listeners],
  );

  useEventStream({
    onTaskMessage: handleTaskMessage,
    onStatusChange: setStatus,
  });

  return (
    <EventStreamContext.Provider value={{ status, onTaskMessage }}>
      {children}
    </EventStreamContext.Provider>
  );
}
