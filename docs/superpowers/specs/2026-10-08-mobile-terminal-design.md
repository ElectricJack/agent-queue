# Shared dashboard terminal — design

Tasks `keen-meadow-54` and `fleet-pinnacle-21`, updated 2026-10-09.
Jack's clarification supersedes the phone-specific UI from the
[2026-09-26 design](2026-09-26-mobile-interactive-terminal-design.md): every
agent terminal window must use the host shell's terminal, on phones and desktops.
The host shell's appearance and controls are the reference.

## One component at every entry point

[`InteractiveTerminal.tsx`](../../../dashboard/src/components/InteractiveTerminal.tsx)
is the only production component that constructs an xterm. HostShell,
AgentTerminal, PoolInstanceTerminal, SessionDetail's Pane tab, FocusSession,
task terminal links, and the session-peek pane all use it. There is no compact
renderer branch, phone input bar, key strip, font picker, fullscreen terminal
sheet, watch/type toggle, or snapshot-renderer fallback.

The original host shell options stay: 12 px monospace, line height 1.2,
blinking cursor, 2,000 lines of scrollback, background `#0d1117`, foreground
`#d1d5db`, and its explicit ANSI palette. `terminalSetup.ts` owns the options
and theme. OSC 52 and OSC 8 output handlers remain disabled. The viewport
background override avoids the black band xterm.css otherwise paints.

The toolbar keeps Type (focus), Enter, Ctrl+C, connection state, details and
Reconnect now. Typing and paste use xterm's own textarea and attach socket.
Ctrl+M releases keyboard focus. Type moves focus; it is not a mode switch.
Input is enabled only after the terminal connection is ready. Transport
recovery follows [terminal-reconnection](../../specs/terminal-reconnection.md).
A refused attach reports the refusal and disables input. An ended session
links to its transcript; it does not open a second terminal renderer.

## Fit, keyboard and touch

The host shell's FitAddon, bounded `terminalDimensions`, ResizeObserver and
font-ready refit apply everywhere. Every measured cols/rows update is sent to
the attached session's PTY and tmux window.

On a touchscreen, `terminalTouchScroll.ts` captures a single-finger drag,
scrolls xterm's buffer by whole lines, prevents page scroll, and continues
with decaying momentum after release. A tap focuses xterm's textarea within
touchend. No extra input surface is involved.

A visualViewport resize or pan refits the terminal. At scale 1, its available
height ends at `visualViewport.offsetTop + visualViewport.height`, measured
from the terminal area's own top. This accounts for page and pane headers,
including a keyboard already open at mount. Pinch zoom does not resize tmux.
The desktop layout and font remain those of the host shell.

## Compact attach options

Below 768 px, the shared component asks for `history=2000` and
`restore_size=1`. Wide attaches omit both. These are transport options, not
a different UI. The attach's initial policy is retained through rotation and
reconnection, while cols/rows continue following the actual terminal area.

`src/sessions/terminal_pty.py` implements the two options:

- History captures up to N lines (`capture-pane -p -e -J`, bounded to 4 MiB)
  after generation checks, sends them ahead of the live screen under output
  credit, and appends `xterm-256color:indn@` once to terminal-overrides so
  tmux scrolls with line feeds. The compact viewer consumes DECSET/DECRST
  47, 1047 and 1049 to keep those lines in xterm's normal buffer. Reconnect
  resets the terminal before sending fresh history.
- Restore size records the pre-attach window dimensions and policy in
  `@aq_restore_size`, tracks attached phone client pids, and restores the
  original dimensions and policy when the last viewer leaves. A second phone
  keeps the first live record; a desktop still attached keeps its own size.

Malformed options are refused with 4400. `/ws/terminal/{id}/input` and the
pane SSE endpoint remain available for older dashboards and external clients;
the new dashboard has no input-only hook or pane-snapshot terminal path.

## Verification

`check:terminal-parity` uses Playwright with iPhone 14, iPhone SE and Pixel 7
mobile contexts against the built SPA and an isolated stub daemon. It checks
computed theme, ANSI/256/truecolor, toolbar parity, absence of a mode switch,
actual attach/resize frames, rotation, a simulated on-screen keyboard,
keyboard typing, touch reaching the first history line, and every agent
terminal entry point, including session-peek. It saves side-by-side host shell
and agent screenshots and a JSON report.

The existing layout harness checks the four mobile sizes plus desktop and
200% zoom for terminal headers, focus/session/agent/pool routes and compact
shell navigation. Vitest verifies options, transport recovery, fit, touch,
keyboard offsets/pan/zoom, and route selection.

The server's unchanged contracts are covered by `tests/test_terminal_stream.py`
and real isolated tmux checks in `tests/test_terminal_pty.py`. No backend
change is required here. Browser stubs prove frames and rendering, not real
PTY execution. The keyboard is simulated; physical iPhone Safari and Android
Chrome remain the operator's release check.

See [the evidence bundle](../../reports/mobile-terminal-host-shell-parity-2026-10-09/README.md)
for checks, screenshots and the deployment investigation.

## Deployment finding and limits

A source merge alone does not replace the served SPA. The installed dashboard
serves `src/dashboard_assets/dist`; `aq update` or
`aq install --restart-from dashboard.build` builds/stages that bundle. The
prior investigation recorded assets older than `keen-meadow-54`, explaining
why its source changes were invisible. The session-peek snapshot renderer was
also a separate unchanged path; it is now removed.

A program drawing in its own alternate screen has no tmux history. Line feeds
inside a split pane's scroll region may not enter scrollback. The server's
indn override changes how all xterm-256color clients receive scrolling.
