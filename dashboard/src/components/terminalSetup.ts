import type { IDisposable, ITheme, Terminal } from "@xterm/xterm";

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

/** Fewer columns than this and a TUI's own layout breaks; the phone shrinks its font first. */
export const MIN_COLUMNS = 40;
/** Text smaller than this is not readable on a phone, so the columns give way instead. */
export const MIN_FONT_SIZE = 10;

/**
 * The font size for a terminal: the viewer's choice, or the largest smaller
 * size that still fits MIN_COLUMNS, never below MIN_FONT_SIZE. `columnsAt`
 * sets a size on the terminal and returns the columns it yields there (null
 * while the terminal cannot be measured). The host's width does not depend on
 * the font, so the same width always settles on the same size.
 */
export function fitFontSize(preferred: number, columnsAt: (fontSize: number) => number | null): number {
  let font = preferred;
  let cols = columnsAt(font);
  while (cols !== null && cols < MIN_COLUMNS && font > MIN_FONT_SIZE) {
    // Columns scale with 1 / font size; the floor and the step guard rounding.
    font = Math.max(MIN_FONT_SIZE, Math.min(font - 1, Math.floor((font * cols) / MIN_COLUMNS)));
    cols = columnsAt(font);
  }
  return font;
}
