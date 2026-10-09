import { useId, useLayoutEffect, useMemo, useState, type RefObject } from "react";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { encodeEntry, MAX_ENTRY_BYTES } from "./terminalInput";

/** About five lines; longer entries scroll inside the box. */
const MAX_HEIGHT_PX = 128;

/**
 * The phone's line editor for an agent terminal. Enter (the keyboard's Send
 * key) sends the entry and then Enter; Shift+Enter adds a line; a pasted block
 * keeps its lines and goes as one bracketed paste. 16 px text, so iOS does not
 * zoom on focus. The draft survives a dropped connection; Send waits for it.
 */
export default function TerminalInputBar({ name, connected, onSubmit, inputRef, onFocusChange }: {
  name: string;
  connected: boolean;
  onSubmit: (text: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  onFocusChange?: (focused: boolean) => void;
}) {
  const [text, setText] = useState("");
  const hintId = useId();
  const size = useMemo(() => encodeEntry(text).byteLength, [text]);
  const tooLong = size > MAX_ENTRY_BYTES;
  const canSend = connected && text.length > 0 && !tooLong;

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight) el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [text, inputRef]);

  const submit = () => {
    if (!canSend) return;
    onSubmit(text);
    setText("");
  };

  return (
    <form
      className="flex shrink-0 flex-col gap-1 border-t border-gray-800 bg-gray-950 px-2 py-2"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="flex items-end gap-2">
        <textarea
          ref={inputRef}
          rows={1}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            submit();
          }}
          onFocus={() => onFocusChange?.(true)}
          onBlur={() => onFocusChange?.(false)}
          enterKeyHint="send"
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          aria-label={`${name} terminal input`}
          aria-describedby={hintId}
          aria-invalid={tooLong || undefined}
          placeholder={connected ? "Type to the agent…" : "Connecting…"}
          className="min-h-11 min-w-0 flex-1 resize-none overflow-y-auto rounded border border-gray-700 bg-black px-2 py-2 font-mono text-base leading-snug text-gray-100 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="submit"
          data-primary-control
          aria-label={`Send to ${name}`}
          disabled={!canSend}
          onMouseDown={(event) => event.preventDefault()}
          className="inline-flex h-11 shrink-0 items-center justify-center gap-1 rounded bg-indigo-600 px-3 text-sm font-medium text-white hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-500"
        >
          <PaperAirplaneIcon className="h-4 w-4" />
          <span>Send</span>
        </button>
      </div>
      <p id={hintId} className="sr-only">Enter sends the line and presses Enter. Shift+Enter adds a line.</p>
      {tooLong && (
        <p role="alert" className="text-xs text-red-400">
          Too long to send: {Math.ceil(size / 1024)} KiB of a {MAX_ENTRY_BYTES / 1024} KiB limit.
        </p>
      )}
    </form>
  );
}
