import { TERMINAL_KEYS, type TerminalKey } from "./terminalInput";

const KEY =
  "inline-flex h-10 min-w-10 flex-auto shrink-0 items-center justify-center whitespace-nowrap rounded border border-gray-700 bg-gray-900 px-2 font-mono text-sm text-gray-100 hover:bg-gray-800 active:bg-gray-700 disabled:opacity-40";

/**
 * The keys a phone keyboard lacks: Esc, Tab, Ctrl-C, arrows, Enter, and 1–3
 * for numbered permission and choice prompts. One row that scrolls sideways on
 * a narrow phone. A tap never takes focus (mousedown is cancelled), so the
 * input bar keeps it and the on-screen keyboard stays up.
 */
export default function TerminalKeyStrip({ name, disabled, onKey }: {
  name: string;
  disabled: boolean;
  onKey: (key: TerminalKey) => void;
}) {
  return (
    <div
      role="toolbar"
      aria-label={`${name} terminal keys`}
      data-allow-overflow-x
      className="flex shrink-0 gap-1 overflow-x-auto border-t border-gray-800 bg-gray-950 px-2 py-1"
    >
      {TERMINAL_KEYS.map((key) => (
        <button
          key={key.name}
          type="button"
          data-primary-control
          aria-label={`Send ${key.name}`}
          disabled={disabled}
          className={KEY}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onKey(key)}
        >
          {key.label}
        </button>
      ))}
    </div>
  );
}
