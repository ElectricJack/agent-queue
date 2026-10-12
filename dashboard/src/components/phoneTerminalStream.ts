import type { IDisposable, Terminal } from "@xterm/xterm";

/**
 * What a terminal that asks tmux for scrollback does to its output, so earlier
 * output stays reachable by scrolling. tmux attaches with `smcup`, which puts
 * xterm.js in its alternate buffer — a buffer with no scrollback. A terminal
 * that asked for history keeps tmux in the normal buffer, where each line a
 * line feed pushes off the top lands in scrollback. (Such an attach also asks
 * for `history`, which makes tmux scroll with line feeds rather than `CSI n S`,
 * whose lines xterm.js would drop.) A terminal that asked for neither keeps the
 * alternate buffer, so full-screen programs inside tmux draw as tmux means.
 */

/** DECSET/DECRST 47, 1047 and 1049: the alternate screen, as tmux's `smcup`/`rmcup` send it. */
const ALT_SCREEN = new Set([47, 1047, 1049]);

/** Keep tmux in the normal buffer, whose lines scroll into scrollback. */
export function swallowAltScreen(terminal: Terminal): IDisposable[] {
  const swallow = (params: (number | number[])[]) => params.length === 1 && ALT_SCREEN.has(params[0] as number);
  return [
    terminal.parser.registerCsiHandler({ prefix: "?", final: "h" }, swallow),
    terminal.parser.registerCsiHandler({ prefix: "?", final: "l" }, swallow),
  ];
}
