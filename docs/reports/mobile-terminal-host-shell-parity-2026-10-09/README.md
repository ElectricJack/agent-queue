# Agent terminals reuse the host shell (2026-10-09)

<!-- aq:historical -->
> Browser evidence for `fleet-pinnacle-21`, following Jack's clarification that
> every agent terminal must reuse the host shell's component and wiring on
> desktop and mobile. This replaces the earlier evidence for the superseded
> phone controls. See the [design](../../superpowers/specs/2026-10-08-mobile-terminal-design.md)
> and [real-phone checklist](../../guides/dashboard.md#checking-a-release-on-a-real-phone).

## Implementation and entry points

`InteractiveTerminal.tsx` is the only production component constructing xterm.
HostShell, AgentTerminal, PoolInstanceTerminal, FocusSession, SessionDetail's
Pane tab and session-peek all render it. Its original 12 px font, palette,
2000-line scrollback, Type focus button, details controls, fit logic and raw
socket input are shared. There is no watch/type toggle, phone input bar, key
strip, font control, fullscreen sheet or snapshot-only terminal renderer.

The previous phone breakpoint path and its unused helpers are removed.
Session-peek was an additional path missed by the original mobile change:
it used `LivePaneConsole` to paint capture-pane snapshots, without the host
shell's terminal behavior. It now opens the same interactive attachment.

Compact attachments ask for `history=2000` and `restore_size=1` and preserve
scrollback in the normal buffer. Both the host shell and the agent terminal
use these options on phones; wide attachments retain the existing defaults.
Touch dragging scrolls the shared buffer. Keyboard coverage caps the terminal
at `visualViewport.offsetTop + height`, subtracting the terminal's actual top,
and sends its fitted rows and columns to the PTY. Without keyboard coverage,
terminals below the fold retain their normal layout; pinch zoom does not
trigger a keyboard cap.

## Reproduce the browser evidence

Playwright Chromium runs against the production SPA and the isolated layout
stub on an ephemeral localhost port. No live daemon or agent receives input.
The fake PTY emits 300 numbered history lines, separate SGR samples for ANSI,
indexed and truecolor foregrounds and a colored background, then a width ruler.

```bash
npm -w dashboard run build
npm -w dashboard run check:terminal-parity -- --out /tmp/terminal-parity
npm -w dashboard run check:layout -- --only terminal-parity,phone-terminal,agents-terminal,focus-session,terminal-headers --out /tmp/terminal-layout
```

| Check | Result |
|---|---|
| Playwright terminal acceptance | 3/3 passed: iPhone 14 (390 × 844), Android (412 × 915), iPhone SE (320 × 568) |
| Existing terminal layout checks | 23/23 passed: phones, landscape, desktop and 200% zoom |
| Focused Vitest terminal checks | 9 files / 115 tests passed |
| Vitest component, agent, focus, session, pane and socket area checks | 52 files / 516 tests passed |
| Production build and typecheck | Passed |
| ESLint | 0 errors; 37 existing react-refresh warnings |

The Playwright runner opens the host shell, focus session, flock agent, pool
instance, full session Pane tab and session-peek drawer at each device size.
It compares their computed fonts and palettes, checks shared controls and the
absence of phone controls, matches drawn rows/columns to attachment dimensions,
rotates and restores the viewport, simulates keyboard coverage and restoration,
types raw input into xterm, and flicks back to history line 001 without scrolling
the page. It also verifies a docked terminal stays above the keyboard.
`playwright-report.json` records the browser version and initial/keyboard grid sizes.

## Screenshots

| Artifact | Shows |
|---|---|
| [iPhone 14 comparison](side-by-side-phone-390.png) | Host shell and focus-session terminal at 390 × 844. |
| [Android comparison](side-by-side-android-412.png) | Same pair at 412 × 915. |
| [iPhone SE comparison](side-by-side-phone-320.png) | Same pair at 320 × 568. |
| [Keyboard coverage](phone-390-agent-terminal-keyboard.png) | Fewer terminal rows above the simulated keyboard boundary. |
| [Earlier output](phone-390-agent-terminal-scrolled-back.png) | History line 001 reached with touch flicks. |

Individual host-shell and agent-terminal images accompany each comparison.
Page headers and session names differ; the terminal toolbar, options and
rendering are the same component. Screenshots come directly from Playwright,
with the exact page PNGs composed side by side by Chromium.

## Why the original deployment appeared unchanged

The earlier worker recorded that its installed `src/dashboard_assets/dist`
was dated 2026-09-26 and its local `dashboard/dist` predated the original
`keen-meadow-54` merge, including a deleted WatchTerminal chunk. That is
historical evidence from the retained task, not a claim about the operator's
current deployment or this fresh worker slot.

The source confirms the deployment mechanism: `src/dashboard_server/bundle.py`
serves a built SPA from `src/dashboard_assets/dist`; pulling dashboard sources
alone cannot update that JavaScript. `src/install/dashboard.py` compares the
source fingerprint in `bundle_is_current()` during install/update.
`dashboard.server.bundle` in `src/doctor/dashboard_server_checks.py` checks the
manifest and base path without comparing that source fingerprint, so its OK
result alone does not establish freshness. In addition to the stale-bundle
finding, session-peek's separate snapshot renderer was a source-level path
that this implementation removes.

Seeing the change on the operator's installed dashboard requires its normal
build/staging step, such as `aq install --restart-from dashboard.build`, followed
by checking the served assets. This worker built and tested the isolated SPA;
it did not rebuild or restart the operator's deployment.

## Limits

These are mobile Chromium viewport/device emulations, not real iOS Safari or
Android hardware. Keyboard coverage is simulated through visualViewport and
touch gestures use Chromium's input protocol. The fake PTY proves browser
sizing messages and input, not a real tmux window; existing terminal transport
tests cover that server behavior. The guide retains the real-device checklist.
