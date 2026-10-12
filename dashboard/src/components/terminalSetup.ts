import type { IDisposable, ITerminalOptions, ITheme, Terminal } from "@xterm/xterm";

/**
 * What every dashboard terminal looks like: the desktop terminal and the phone
 * terminal render the same tmux bytes with this one theme, so a colour on a
 * phone is the colour on a desktop. The sixteen ANSI colours are xterm.js's
 * own defaults, written out so the palette is a contract rather than whatever
 * the library ships next. xterm.css paints `.xterm-viewport` #000 whatever
 * the theme says, so each host also sets `[&_.xterm-viewport]:bg-[#0d1117]!`.
 */
export const TERMINAL_BACKGROUND = "#0d1117";

export const TERMINAL_THEME: ITheme = {
  background: TERMINAL_BACKGROUND,
  foreground: "#d1d5db",
  cursor: "#e5e7eb",
  selectionBackground: "#6366f14d",
  black: "#2e3436",
  red: "#cc0000",
  green: "#4e9a06",
  yellow: "#c4a000",
  blue: "#3465a4",
  magenta: "#75507b",
  cyan: "#06989a",
  white: "#d3d7cf",
  brightBlack: "#555753",
  brightRed: "#ef2929",
  brightGreen: "#8ae234",
  brightYellow: "#fce94f",
  brightBlue: "#729fcf",
  brightMagenta: "#ad7fa8",
  brightCyan: "#34e2e2",
  brightWhite: "#eeeeec",
};

export const TERMINAL_FONT_FAMILY = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace';
export const TERMINAL_LINE_HEIGHT = 1.2;
/** The host shell's fixed font size, used by every terminal. */
export const TERMINAL_FONT_SIZE = 12;
/** Preserve the host shell's existing scrollback limit. */
export const TERMINAL_SCROLLBACK = 2000;

/**
 * The xterm options every dashboard terminal is built from. One factory, so the
 * host shell page's remote shell, an agent or session window's terminal and the
 * phone's are the same terminal: the same font, line height, palette,
 * scrollback and input policy.
 */
export function terminalOptions(fontSize: number = TERMINAL_FONT_SIZE): ITerminalOptions {
  return {
    fontFamily: TERMINAL_FONT_FAMILY,
    fontSize,
    lineHeight: TERMINAL_LINE_HEIGHT,
    cursorBlink: true,
    scrollback: TERMINAL_SCROLLBACK,
    // Input is enabled only after the attach is ready.
    disableStdin: true,
    logLevel: "off",
    theme: TERMINAL_THEME,
  };
}

/**
 * Terminal output is untrusted. Keep manual copy/paste, but never let escape
 * sequences access the clipboard (OSC 52) or activate remote hyperlinks (OSC 8).
 */
export function guardUntrustedOutput(terminal: Terminal): IDisposable[] {
  return [
    terminal.parser.registerOscHandler(52, () => true),
    terminal.parser.registerOscHandler(8, () => true),
  ];
}
