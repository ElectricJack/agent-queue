import { useId, useRef, type KeyboardEvent } from "react";
import { useTerminalInput } from "../api/useTerminalInput";
import type { PaneStatus } from "../ws/usePaneStream";
import LivePaneConsole from "./LivePaneConsole";

const keys: Record<string, string> = {
  Enter: "Enter", Backspace: "BSpace", Tab: "Tab", Escape: "Escape",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  Home: "Home", End: "End", PageUp: "PPage", PageDown: "NPage", Delete: "DC",
};
const controlKeys = new Set(["a", "b", "c", "d", "e", "f", "j", "k", "l", "n", "p", "u", "w", "z"]);

export default function InteractiveTerminal({ sessionId, name, screen, status, error }: {
  sessionId: string;
  name: string;
  screen: string | null;
  status: PaneStatus;
  error?: string | null;
}) {
  const available = status === "open";
  const input = useTerminalInput(sessionId, available);
  const disabled = !available || !!input.error;
  const consoleRef = useRef<HTMLDivElement>(null);
  const focusButton = useRef<HTMLButtonElement>(null);
  const hintId = useId();

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) {
      event.stopPropagation();
      return;
    }
    if (event.nativeEvent.isComposing) return;
    if (event.ctrlKey && event.key.toLowerCase() === "m") {
      event.preventDefault();
      event.stopPropagation();
      focusButton.current?.focus();
      return;
    }
    if (event.metaKey || event.altKey) return;
    let key: string | undefined;
    if (event.ctrlKey) {
      const letter = event.key.toLowerCase();
      // Preserve native copy when output is selected. The Interrupt button
      // always sends Ctrl+C, independently of the browser's text selection.
      if (letter === "c" && window.getSelection()?.toString()) return;
      if (event.shiftKey || !controlKeys.has(letter)) return;
      key = "C-" + letter;
    } else {
      key = event.shiftKey && event.key === "Enter" ? "C-j"
        : event.shiftKey && event.key === "Tab" ? "BTab" : keys[event.key];
    }
    if (key) {
      event.preventDefault();
      event.stopPropagation();
      input.write({ key });
    } else if (!event.ctrlKey && Array.from(event.key).length === 1) {
      event.preventDefault();
      event.stopPropagation();
      input.write({ text: event.key });
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={consoleRef} role="textbox" aria-label={name + " terminal input"}
        aria-multiline="true" aria-describedby={hintId} aria-readonly={disabled} aria-disabled={disabled}
        tabIndex={disabled ? -1 : 0}
        onClick={() => { if (!disabled) consoleRef.current?.focus(); }}
        onKeyDown={onKeyDown}
        onCompositionEnd={(event) => {
          if (!disabled && event.data) input.write({ text: event.data });
        }}
        onPaste={(event) => {
          if (disabled) return;
          event.preventDefault();
          event.stopPropagation();
          input.write({ text: event.clipboardData.getData("text/plain") });
        }}
        className="min-h-0 flex-1 overflow-hidden outline-none focus:ring-1 focus:ring-inset focus:ring-indigo-400">
        <LivePaneConsole screen={screen} status={status} error={error} className="h-full" />
      </div>
      {input.error && (
        <div role="alert" className="shrink-0 border-t border-red-900 bg-red-950/20 px-3 py-2 text-xs text-red-300">
          <p>{input.error} Unsent input was discarded. Check the terminal before continuing.</p>
          <button type="button" disabled={!available}
            onClick={() => input.resume()}
            className="mt-2 rounded border border-red-800 px-2 py-1 hover:bg-red-900/30 disabled:opacity-40">
            Enable input
          </button>
        </div>
      )}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-gray-800 px-3 py-2 text-[10px] text-gray-500">
        <span id={hintId} className="min-w-0 flex-1">Click to type · Ctrl+M releases keyboard · Paste never submits</span>
        {input.pending > 0 && <span>{input.pending} queued</span>}
        <button ref={focusButton} type="button" aria-label={"Focus " + name + " terminal"} disabled={disabled}
          onClick={() => consoleRef.current?.focus()}
          className="rounded border border-gray-700 px-2 py-1 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Type</button>
        <button type="button" aria-label={"Send Enter to " + name} disabled={disabled}
          onClick={() => input.write({ key: "Enter" })}
          className="rounded border border-gray-700 px-2 py-1 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Enter</button>
        <button type="button" aria-label={"Interrupt " + name} disabled={disabled}
          onClick={() => input.write({ key: "C-c" })}
          className="rounded border border-gray-700 px-2 py-1 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Ctrl+C</button>
      </div>
    </div>
  );
}
