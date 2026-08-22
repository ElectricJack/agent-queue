import { useCallback } from "react";
import { useEventStream } from "../ws/useEventStream";
import type { NotifyEvent } from "../ws/types";
import { useShellPaneStore } from "./store";

export function useAgentPushBridge(): void {
  const { open, registry } = useShellPaneStore();
  useEventStream({
    onEvent: useCallback(
      (event: NotifyEvent) => {
        if (event.event_type !== "message.sent") return;
        const toKind = (event as { to_kind?: string }).to_kind;
        if (toKind !== "user") return;
        const paneOpen = (event as { pane_open?: { view: string; args: unknown } | null })
          .pane_open;
        if (!paneOpen) return;
        const entry = registry[paneOpen.view];
        if (!entry) {
          console.debug(`agent-push: unknown view "${paneOpen.view}", ignoring`);
          return;
        }
        if (entry.manifest.agent_pushable === false) {
          console.debug(`agent-push: view "${paneOpen.view}" not pushable, ignoring`);
          return;
        }
        open(paneOpen.view, paneOpen.args);
      },
      [open, registry],
    ),
  });
}
