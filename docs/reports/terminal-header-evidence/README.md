# Compact terminal pane headers

The agent, supervisor, pool, standalone session, and phone terminal now use one
header row. It contains a truncated name, compact status, typing control, details
button, and the applicable full-screen/close controls. Desktop rows are 37 px;
phone/touch rows are 49 px, including 44 px control targets. Terminal text remains
12 px by default, with the existing 12–20 px phone font adjustment.

Details expose the complete task/profile/model/session information, pool supply,
instance picker, settings tabs, supervisor restart choice, terminal Enter/Ctrl+C,
font adjustment, and reconnect information/actions. Click, touch, or keyboard
activation opens the disclosure. Escape closes it and restores trigger focus;
clicking outside lets the terminal take focus. Disclosure does not remount xterm
or change its transport. Watch mode still starts without an input socket; its
single Type/Watch control opens/closes the existing input-only socket.

The fixtures show three panes together, with long mixed-script titles, two live
PTYs and a refused desktop connection. Below 768 px they use the existing pane
stream instead of attaching. The phone screenshot shows the scrollable stack;
its second image shows the third pane. All data and sessions are synthetic.

| Viewport | Pane | Header before → after | Rendered terminal before → after |
| --- | --- | --- | --- |
| 1440×900 | Worker | 107 → 37 px | 249 → 349 px |
| 1440×900 | Supervisor | 142 → 37 px | 214 → 349 px |
| 1440×900 | Pool | 186 → 37 px | 170 → 349 px |
| 1100×700 | Worker | 137 → 37 px | 119 → 249 px |
| 1100×700 | Supervisor | 189 → 37 px | 58 → 249 px |
| 1100×700 | Pool | 237 → 37 px | 19 → 249 px |
| 390×844 | Worker | 152 → 49 px | 69 → 269 px |
| 390×844 | Supervisor | 204 → 49 px | 39 → 269 px |
| 390×844 | Pool | 269 → 49 px | 39 → 269 px |

The phone gain also fixes the watch output container so it fills the space the
shorter header frees. Pane dimensions and default terminal font sizes are unchanged.
Exact measurements are in [before.json](before.json) and [after.json](after.json).
[Browser results](layout-report.json): 18/18 checks passed.
The terminal/agents/session/focus area suite passed 288/288 tests in 18 modules;
repository storage, pane registry, and selection catalogue ratchets passed 42/42.
Production build, typecheck, and changed-file ESLint passed.
The before bundle was built from source base `bd35e46f0fe603ff17e8ad241ead6e33e2174c88`
using the same fixture and capture harness; it is the recorded baseline, not a
newly measured substitute for a failing test.

## Screenshots

| Layout | Before | After |
| --- | --- | --- |
| Desktop, three panes | [Before](before-desktop.png) | [After](after-desktop.png) |
| Small desktop, three panes | [Before](before-small.png) | [After](after-small.png) |
| Phone stack | [Before](before-phone.png) | [After](after-phone.png) |
| Phone, third pane | [Before](before-phone-pool.png) | [After](after-phone-pool.png) |

[Keyboard-opened details](after-keyboard-details.png) and
[touch-opened pool details](after-pool-details.png) show the disclosure.

## Verification and reproduction

Build before running browser checks, so Vite does not replace assets during a check:

```sh
npm -w dashboard run typecheck
npm -w dashboard run build
npm -w dashboard run check:layout -- --only terminal-headers,agents-watch-only,focus-session,phone-typing
node dashboard/layout-checks/capture-terminal-headers.mjs after
```

`terminal-headers` checks every supported viewport (320×568, 390×844, 844×390,
1440×900, and 720×450 at 200% zoom), plus the 1100×700 three-pane layout. It
asserts one row per pane, increased terminal height, unclipped named controls,
no horizontal overflow, complete details, keyboard activation/Escape, touch
activation, viewport-contained disclosures, unchanged viewer count, and terminal
focus/Ctrl+M without input. Existing layout checks cover font changes, stale
screens/retry, full screen/rotation/Back/focus traps, and phone input frames.

Component checks cover disclosure keyboard navigation and action activation,
outside-click focus, transport/reconnect/resize/selection, pool instance switching,
settings, supervisor restart, session lifecycle guards, and phone input behavior.
See the task completion evidence for the exact Vitest command and results.
