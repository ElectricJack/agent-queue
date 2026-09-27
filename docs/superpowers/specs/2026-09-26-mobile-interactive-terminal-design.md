# Mobile interactive terminals — design

Task `bright-crest`, 2026-09-26. Follows the mobile dashboard work (quick-rapids),
which made every terminal below 768 px watch-only
([dashboard guide, From a phone](../../guides/dashboard.md#from-a-phone)).

## Problem

The operator wants to answer agents from a phone: type a line, press Esc or
Ctrl-C, pick option 2 in a permission dialog. Phones only get the watch-only
pane stream today, because an interactive terminal is a tmux **attach**. Agent
windows run with `window-size latest`, so an attached client sizes the agent's
real window for everyone.

Evidence (tmux 3.4, private socket, a 200×50 detached session):

| Phone client | Window after attach | After typing | After the phone leaves |
|---|---|---|---|
| `attach-session -f ignore-size` at 45×30 | 45×29 | 45×29 | **45×29** |

`ignore-size` does not help. tmux ignores a flagged client only while an
unflagged client is attached, and agents normally run detached. A phone
attach therefore squeezes the agent to phone width, and the window keeps that
width after the phone leaves. Attaching at the window's current size is fragile
too: it needs the status-line height, it races desktop resizes, and it restores
a stale size when the last desktop viewer detaches.

## Decision

A phone never attaches. It types through an **input-only terminal socket**,
`/ws/terminal/{session_id}/input`. The socket is served by the same
`TerminalStreamService.handle()` as the attach socket, with `input_only=True`.
It creates no tmux client, so it cannot resize anything. The phone keeps
watching the pane stream it already uses (`usePaneStream` → `LivePaneConsole`).

### Why a sibling route and not `?mode=input` or `session_input`

* **Same gates, by construction.** The path is under `/ws/terminal/`, so the
  dashboard server's Host, Origin and loopback-only peer gates apply
  (`src/dashboard_server/edge.py`). The daemon runs the same `_check_origin`,
  `_credentials`, global-operator `_authorize`, session-generation fence,
  connection limit and 2 s monitor re-checks as on the attach socket.
* **Safe on version skew.** A daemon that predates this route refuses the
  handshake. With a query parameter, an old daemon would ignore the flag and
  attach at phone size, which is exactly the failure above.
* **Not `session_input`.** That command is reached over `/api`, where the edge
  applies no loopback-only peer rule. Using it from the phone would be a write
  path that bypasses the terminal rules.

### Protocol (`aq-terminal-v1`, input-only)

* The handshake takes no `cols` / `rows`. The ready frame is
  `{"type": "ready", "session_id": …, "mode": "input"}`. The browser refuses a
  ready frame without `mode: "input"`.
* Binary frames are input bytes, with the same 64 KiB frame and 128 KiB queue
  limits. `ping` → `pong` works as before. The server never sends output bytes,
  so there is nothing to `ack`. A `resize` control is refused
  (`4400 Invalid terminal control`).
* Session end or change is detected by the monitor loop, as on the attach
  socket (`4409`).

### Server: `TmuxInputClient` (`src/sessions/terminal_input.py`)

It has the attach client's interface (`read`, `write`, `resize`, `verify`,
`close`):

* **Fence.** It resolves the tmux session id (`$N`) with the instance-token
  check the PTY client uses, and it re-verifies before **every write**. A stale
  phone must never type into a same-named successor, or into a restarted tmux
  server whose `$N` was reused.
* **Keystrokes** go through `send-keys -t $N -H <hex bytes…>`, which writes
  raw bytes to the session's active pane, the one an attached client types
  into. A frame that is exactly one arrow key (`ESC [ A`…`D` or `ESC O A`…`D`)
  is sent by key name instead, so tmux encodes it for the pane's cursor-key
  mode, as it does for an attached client.
* **Pastes.** Bytes between `ESC [200~` and `ESC [201~` are collected, even
  across frames, up to 64 KiB, then loaded into a unique tmux buffer and
  inserted with `paste-buffer -p -d`. tmux adds bracketed-paste markers only
  when the application asked for them, and turns LF into CR. An attached tmux
  client and a desktop paste behave the same way.
* **Errors** never include input or tmux argv. A paste over the limit is
  `4400`.
* `read()` produces nothing until `close()`.

### Dashboard

* `connectTerminal({ mode: "input" })` (`dashboard/src/ws/terminalSocket.ts`):
  the `/input` path, no dimensions, no `resize`, and output treated as a
  protocol error. Reconnect, keepalive and the HTTP access probe are shared
  with the attach mode.
* `WatchTerminal` stays the phone terminal. A **Watch only / Typing** toggle
  (`aria-pressed`) starts in Watch only on every mount and is never persisted
  (dashboard storage rules). Only Typing opens the input socket, and switching
  back closes it.
* **Input bar** (`TerminalInputBar`): an auto-growing textarea with
  `enterkeyhint="send"`.
  * Enter sends and Shift+Enter adds a newline. IME composition never sends.
  * Pasted text keeps its newlines. A multi-line entry is sent as one
    bracketed paste.
  * Send writes the text, then Enter as a separate write about 150 ms later.
    TUIs treat a burst that contains CR as a paste and would swallow the
    submit.
  * The limit is 64 KiB. It is disabled while the socket is not connected.
* **Key strip** (`TerminalKeyStrip`): Esc, Tab, Ctrl-C, ↑, ↓, Enter, 1, 2, 3,
  each at least 44 px, in one row that scrolls sideways. Buttons cancel
  `mousedown`, so a tap never takes focus from the input bar and the on-screen
  keyboard stays up.
* **Taps.** With Typing on, a tap on the screen (not a selection or a scroll)
  focuses the input bar. No hidden terminal textarea ever takes focus, so the
  keyboard never opens over the output.
* **Keyboard viewport** (`useVisualViewport`). While the input bar has focus
  and the visual viewport is shorter than the layout viewport (the on-screen
  keyboard is up), the terminal is fixed to the visual viewport's rectangle.
  Status, screen, keys and input then fill exactly what the keyboard leaves
  visible, and iOS panning cannot push the input bar behind the keyboard.
* **Follow the bottom.** With Typing on, the screen stays scrolled to the
  bottom (the agent's prompt) on every new frame and viewport change, unless
  the operator has scrolled up.
* **Other watch links.** Focus routes and compact pages keep the pane stream
  and never attach. `TaskAgentTerminalButton` still links phones to the focus
  session, labelled so that it no longer promises watch-only.

## Tests

* `tests/test_terminal_stream.py`: the input route runs the auth, origin and
  generation refusals without creating a client. The ready frame carries
  `mode: "input"`, `resize` is refused, input reaches the client, and no output
  or ack flows.
* `tests/test_terminal_input.py` (real isolated tmux, `tmux` marker): bytes,
  UTF-8, Ctrl-C and Esc reach the pane. A bracketed paste reaches an app that
  asked for bracketed paste with markers, CR-separated. The window size is
  unchanged. A replaced instance refuses the write.
* Vitest: `TerminalInputBar`, `TerminalKeyStrip`, the `WatchTerminal` toggle
  (no socket until Typing, closed again on Watch only), and the input mode of
  `connectTerminal`.
* Layout harness (`dashboard/layout-checks`, headless Chrome at the phone
  profiles): the stub accepts `/ws/terminal/{id}/input` and records its frames.
  The check switches to Typing, sends a line and a key-strip key, and asserts
  that the bytes reached the socket. It also asserts that nothing opened
  `/ws/terminal/{id}` (attach), and it holds the 44 px and overflow rules.

## Out of scope

Desktop terminals are unchanged. Phone typing assumes a trusted-origin setup
(tailnet via `tailscale serve`) exactly as a desktop terminal does from another
machine. The operator verifies on iPhone Safari after delivery.
