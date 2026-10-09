/**
 * What a phone types into an agent's terminal (mobile terminal spec,
 * docs/superpowers/specs/2026-10-08-mobile-terminal-design.md). Bytes only: the
 * phone's attach socket carries them, as keystrokes into its own tmux client.
 */

/** One entry, as the daemon's direct-input path limits a paste. */
export const MAX_ENTRY_BYTES = 64 * 1024;
/**
 * Enter goes as its own write after the text: a TUI reads a burst that ends in
 * CR as a paste and inserts a newline instead of submitting.
 */
export const SUBMIT_DELAY_MS = 150;

export const PASTE_START = "\x1b[200~";
export const PASTE_END = "\x1b[201~";
export const ENTER = "\r";

export interface TerminalKey {
  /** Visible label. */
  label: string;
  /** Accessible name: "Send <name>". */
  name: string;
  bytes: string;
}

/** Keys a phone keyboard lacks, plus 1–3 for numbered permission and choice prompts. */
export const TERMINAL_KEYS: readonly TerminalKey[] = [
  { label: "Esc", name: "Escape", bytes: "\x1b" },
  { label: "Tab", name: "Tab", bytes: "\t" },
  { label: "Ctrl-C", name: "Ctrl-C", bytes: "\x03" },
  { label: "↑", name: "Up arrow", bytes: "\x1b[A" },
  { label: "↓", name: "Down arrow", bytes: "\x1b[B" },
  { label: "Enter", name: "Enter", bytes: ENTER },
  { label: "1", name: "1", bytes: "1" },
  { label: "2", name: "2", bytes: "2" },
  { label: "3", name: "3", bytes: "3" },
];

const encoder = new TextEncoder();

/**
 * The text of one entry. Line endings become LF, and control characters are
 * dropped: keys are the key strip's job, and an ESC inside pasted text could
 * end a bracketed paste early and type the rest as keystrokes.
 */
export function entryText(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\r\n?/g, "\n").replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");
}

/**
 * One line is sent as typed; several lines as one bracketed paste with CR line
 * endings, exactly as xterm.js sends a desktop paste. tmux passes the brackets
 * on only to an app that asked for them.
 */
export function encodeEntry(text: string): Uint8Array {
  const clean = entryText(text);
  return encoder.encode(clean.includes("\n") ? PASTE_START + clean.replace(/\n/g, ENTER) + PASTE_END : clean);
}

export function encodeKey(key: TerminalKey | string): Uint8Array {
  return encoder.encode(typeof key === "string" ? key : key.bytes);
}
