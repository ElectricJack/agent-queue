# Mobile terminal — design

Task `keen-meadow-54`, 2026-10-08. Replaces the phone half of the
[mobile interactive terminal](2026-09-26-mobile-interactive-terminal-design.md)
design: phones now attach, and the Watch only / Typing toggle is gone. The
input bar, key strip and keyboard-viewport handling from that design stay.

## Problem

Jack, 2026-10-08: the phone terminal should look and behave like a terminal.

1. **Colours.** The phone must render the desktop's theme and ANSI palette:
   background, foreground, cursor, 256-colour and truecolor.
2. **No mode toggle.** A tap types; a drag scrolls. Read-only stays read-only
   where the session itself is.
3. **Size.** Columns and rows follow the phone (portrait, landscape, keyboard
   up or down), and tmux follows them, with a sensible minimum.
4. **Scrollback.** Earlier output is reachable by touch, with momentum, without
   the page stealing the gesture.

### Where the phone diverged

The phone terminal (`WatchTerminal`) was not a terminal. It drew pane snapshots
from the pane stream through `LivePaneConsole`:

| | Desktop (`InteractiveTerminal`) | Phone (`WatchTerminal`) |
|---|---|---|
| Renderer | xterm.js, attach socket | `ansiToSpans` into a `<pre>` |
| Background / text | `#0d1117` / `#d1d5db` | `bg-black` / `text-green-200` |
| Colours | xterm's 16 + 256 + truecolor | its own 16 SGR colours; 256 and truecolor dropped |
| Width | the viewer's columns | the agent's columns, scrolled sideways |
| History | none either (alternate buffer) | the visible screen only |
| Input | the attach socket | the `/input` socket, after **Type** |

Two further findings:

* **Desktop had no scrollback either.** tmux attaches with `smcup`, which puts
  xterm.js in its alternate buffer, and the alternate buffer has no scrollback.
* **xterm.js 6 paints `.xterm-viewport` black** whatever the theme says, so a
  terminal whose rows do not fill its host shows a black band under the last
  row. Both hosts now colour it with the theme background.

## Decision

The phone runs the desktop's terminal: xterm.js on the attach socket
(`/ws/terminal/{id}`), with one shared theme. Two opt-in query parameters make
an attach safe and useful from a phone. Desktop attaches send neither and are
unchanged.

### The size problem, solved instead of avoided

The 2026-09-26 design never attached from a phone because agent windows run
with `window-size latest`: an attach sizes the agent's real window, and tmux
keeps the last size once every client has left. A phone visit left the agent
at phone width. `ignore-size` does not help while the phone is the only client.

`restore_size=1` fixes the leaving half ([`terminal_pty.py`](../../../src/sessions/terminal_pty.py)):

* Before the attach, the client records `"<cols>x<rows> <window-size policy>"`
  in the session option `@aq_restore_size`. Once attached, it appends its tmux
  `client_pid`. A record that names an attached client is live and is kept, so
  a second phone does not record the first phone's size; any other record is
  stale and is replaced.
* When the last client detaches, it runs `resize-window` to the recorded size,
  then sets the recorded `window-size` policy back (the same pair
  `TmuxProvider` repaint uses) and unsets the option.
* A phone that leaves while desktop viewers stay attached unsets the record,
  unless another phone holds it. The desktops keep the window at their size, as
  before any phone.
* Best effort. A failed record means the window keeps the phone's size, which
  is what any attach did before.

While the phone is attached the agent runs at phone width. That is the cost of
a real terminal, and the request. A TUI redraws to the new size; a line-mode
agent wraps.

### Scrollback

`history=N` (0–10 000; the phone asks for 2 000) adds two things:

1. **History prefill.** After the generation checks, the client captures up to
   N lines of the pane's history (`capture-pane -p -e -J`, wrapped lines
   joined, trailing blanks trimmed, at most 4 MiB). Each line ends with an SGR
   reset. Then `rows` line feeds push the last line just above a blank screen,
   which tmux's first redraw paints. The prefill goes out ahead of the attach
   output and under the same output credit.
2. **Line-feed scrolling.** xterm-256color's `indn` (`CSI n S`) scrolls several
   lines at once, and a terminal *discards* lines that `CSI S` removes. With
   `indn` dropped (`terminal-overrides` gets `xterm-256color:indn@`, appended
   once, server-wide), tmux scrolls with line feeds, and each line it pushes
   off the top lands in the viewer's scrollback. Every client still sees the
   same screen. Checked on tmux 3.4: a burst arrives as `CSI 15 S` without the
   override and as line feeds with it.

The client keeps tmux in the normal buffer: the phone terminal swallows
DECSET/DECRST 47, 1047 and 1049 ([`phoneTerminalStream.ts`](../../../dashboard/src/components/phoneTerminalStream.ts)).
Its scrollback holds 8 000 lines. A reconnect writes RIS before the new
prefill, so history is never duplicated.

A rejected alternative rewrote `CSI n S` into line feeds in the browser. It
cannot work: xterm.js saves the DECSC cursor as an absolute buffer row, so a
save, scroll, restore puts the cursor n rows too high until scrollback fills.

### Touch

xterm.js 6 bundles VS Code's touch gestures but registers no target, so a
finger scrolled nothing, or scrolled the page.
[`terminalTouchScroll.ts`](../../../dashboard/src/components/terminalTouchScroll.ts)
listens in the capture phase on the terminal host:

* A drag scrolls the buffer by whole lines and calls `preventDefault`, so the
  page never moves.
* After release it glides on with iOS-like decay (e^(−t/325 ms)), measured over
  the last 100 ms of the drag. A new touch stops the glide.
* A touch that moves less than 8 px and lasts less than 600 ms is a tap. It
  focuses the input bar inside `touchend`, so iOS raises the keyboard. The
  input bar is where typing goes; xterm's own textarea never takes focus.

A ↓ button ("Jump to latest output") shows while the view is scrolled back.
Output that arrives meanwhile does not pull the view down.

### Size and font

* `FitAddon` fits columns and rows to the host. The host fills the phone in
  portrait and in landscape. While the input bar has focus and the keyboard is
  up, `useVisualViewport` pins the terminal to the visible rectangle. In every
  case a `ResizeObserver` refits and a `resize` control reaches tmux, so tmux
  tracks load, rotation and the keyboard opening or closing.
* The font size is the viewer's choice (12–20 px, `A−`/`A+`). If fewer than 40
  columns would fit, the font shrinks, but not below 10 px
  (`fitFontSize`, [`terminalSetup.ts`](../../../dashboard/src/components/terminalSetup.ts)).
  The toolbar shows the size in effect and says when it is capped.

### Colour

[`terminalSetup.ts`](../../../dashboard/src/components/terminalSetup.ts) holds the
one theme: background `#0d1117`, foreground, cursor, and the 16 ANSI colours
written out (xterm.js's own defaults, as a contract). It also holds the font,
line height and the OSC 52 / OSC 8 guard. `InteractiveTerminal` and
`PhoneTerminal` both import it. 256-colour and truecolor come from xterm.js
itself, the same on both.

### Read-only

There is no mode. Watch only remains in two cases, both the session's state,
not a choice:

* **The session has ended.** The last screen stays and input is gone.
* **The daemon refuses the attach** (a 4401/4403 handshake refusal, or the
  access probe answering it). This is a viewer that fails the operator,
  origin or loopback rules. That viewer would be refused typing too, since the
  attach and `/input` routes run the same gates. It watches the pane stream,
  drawn by the same xterm with the same theme, with **Retry** and the last
  screen's age when the stream drops.

### Typing

The input bar, key strip, 150 ms line-then-Enter ordering and keyboard viewport
are unchanged from the 2026-09-26 design. They now write to the attach socket
([`useTerminalTyping.ts`](../../../dashboard/src/ws/useTerminalTyping.ts)). The
attach socket carries raw keyboard bytes, so a multi-line entry is a bracketed
paste with CR line endings, as an xterm.js paste is (`encodeEntry`,
[`terminalInput.ts`](../../../dashboard/src/components/terminalInput.ts)).

The client's input-only socket mode is removed. The daemon keeps
`/ws/terminal/{id}/input` for dashboard builds from before this change, such as
a phone tab still open across an update.

## Tests

* `tests/test_terminal_stream.py`: `history` and `restore_size` parsing and
  refusal (`4400`), the prefill sent first and under credit, attach keywords
  passed only when asked for.
* `tests/test_terminal_pty.py` (real isolated tmux, `tmux` marker): the window
  size and policy come back after the last of two phones leaves, so the second
  did not overwrite the first record. A phone leaving a desktop attached leaves
  no record behind. The history framing is correct. A burst scrolls with line
  feeds once the override is in, and the override is added once.
* Vitest: `PhoneTerminal` (attach query, resize, tap focuses input, no toggle,
  refused fallback, exited read-only, Jump to latest, capped font), the stream
  filter against a real xterm.js parser, touch scroll and momentum,
  `fitFontSize`, the socket's phone query and refusal marks, `encodeEntry`.
* Layout harness (`dashboard/layout-checks`, headless Chrome with touch):
  `phone-terminal` runs at iPhone 320 and 390, Android 412 and landscape. It
  checks: the theme matches the desktop terminal's computed colours; there is
  no toggle; the attach URL carries the phone query; cols and rows fill the
  host and follow rotation and a simulated keyboard; a touch drag reaches the
  first history line with momentum, without page scroll; typing reaches the
  attach socket. `agents-terminal`, `terminal-headers` and `focus-session`
  cover the other phone surfaces and the refused viewer. The run that shipped
  this change, with the sizes it measured and its screenshots, is in
  [`docs/reports/mobile-terminal-2026-10-09/`](../../reports/mobile-terminal-2026-10-09/README.md).

## Limits

* An application drawing in its own alternate screen inside the pane has no
  tmux history. Scrollback then holds only what tmux scrolled while the phone
  watched.
* Line feeds only reach scrollback when the scroll region starts at the top
  row. A pane in a split window, or a status line at the top, scrolls inside a
  region, and those lines are not kept. Agent sessions are single-pane windows,
  and tmux puts the status line at the bottom unless the host's tmux config
  moves it.
* The `indn` override stays on the tmux server. It changes how tmux draws a
  scroll for every xterm-256color client, not what any client sees.
