# Mobile terminal: browser checks at phone sizes (2026-10-09)

<!-- aq:historical -->
> **Evidence, not guidance.** This bundle records what the dashboard's layout
> checks observed on 2026-10-09 for task `keen-meadow-54`, which made the phone
> terminal attach like the desktop one. For the design read
> [the mobile terminal spec](../../superpowers/specs/2026-10-08-mobile-terminal-design.md);
> for using it, [the dashboard guide](../../guides/dashboard.md#checking-a-release-on-a-real-phone).

## What was run

Headless Chrome (puppeteer-core) against a production build of the dashboard,
served by the layout checks' stub daemon (`dashboard/layout-checks/server.mjs`).
The stub accepts a terminal attach for the sessions a check allows, records the
attach URL and every frame the page sends, and writes a fixture screen: 300
numbered history lines, one line in 16-colour, 256-colour, truecolor and
background SGR, then live output.

```bash
cd dashboard
npm run build
npm run check:layout -- --only phone-terminal,agents-terminal,terminal-headers,focus-session --out <dir>
```

| Run | Code | Result |
|---|---|---|
| All four terminal checks | as committed with this bundle | 19/19 passed |
| `phone-terminal`, key-label assertion without the key-strip fix | one class earlier | failed on 320 and 390 ("a key label is wider than its key"); the fix sizes keys to their labels |

The screenshots below come from the 19/19 run.

`phone-terminal` runs on four sizes: iPhone SE (320×568), iPhone 14 (390×844),
an Android phone (412×915, Pixel 7 class) and the iPhone 14 in landscape
(844×390). On each it checks that:

- there is no Type or Watch toggle; the input bar and key strip show at once;
- the attach URL carries `cols`, `rows`, `history` and `restore_size`, and the
  columns and rows fill the terminal area to within one cell;
- rotating sends a resize, and so do opening and closing the keyboard;
- a drag scrolls back and keeps moving after the finger lifts; flicking on
  reaches `history line 001`, 300 lines back; the page itself never scrolls,
  and live output arriving while scrolled back does not pull the view down;
  the ↓ control returns to the bottom;
- a tap focuses the input bar and a drag does not;
- typed text, a multi-line paste and the strip keys go out over the attach;
- the page holds one attach throughout, writes no preferences, and closes the
  attach on leaving;
- the colours, including the viewport behind the last row, equal the desktop
  terminal's for the same session.

## Sizes the check measured

The `cols`×`rows` the phone sent tmux.

| Profile | On load | After rotating | Keyboard up |
|---|---|---|---|
| iPhone 320×568 | 41×21 | 75×6 | 41×10 |
| iPhone 390×844 | 51×37 | 114×10 | 51×20 |
| Android 412×915 | 54×41 | 123×11 | 54×22 |
| iPhone landscape 844×390 | 114×10 | 51×37 | 114×3 |

The keyboard covers 40% of the screen height. In landscape that leaves three
rows above the input bar, which is the trade-off of typing sideways on a phone.

## Screenshots

| File | Shows |
|---|---|
| [iphone-390-live.png](iphone-390-live.png) | A live session at 51 columns: the end of history, the colour sample, the fixture's screen (the `#` rows are the check's width ruler), no toggle |
| [iphone-320-live.png](iphone-320-live.png) | The same at 320 px, 41 columns |
| [android-412-scrolled-back.png](android-412-scrolled-back.png) | Android scrolled back to `history line 001`, with the ↓ control |
| [iphone-390-scrolled-back.png](iphone-390-scrolled-back.png) | The same on the iPhone; the page header has not moved |
| [iphone-390-keyboard.png](iphone-390-keyboard.png) | Keyboard up (the empty lower part is the faked keyboard): the terminal at 20 rows, the key strip and input bar just above the keyboard |
| [android-412-keyboard.png](android-412-keyboard.png) | The same on Android |
| [iphone-390-rotated.png](iphone-390-rotated.png) | After rotating to landscape: 114 columns |
| [landscape-844-live.png](landscape-844-live.png) | Loaded in landscape |
| [iphone-390-theme-phone.png](iphone-390-theme-phone.png), [iphone-390-theme-desktop.png](iphone-390-theme-desktop.png) | One session on the phone terminal, then on the desktop terminal at 1024 px: the same colours |
| [iphone-390-refused-watch-only.png](iphone-390-refused-watch-only.png) | A session the daemon refuses an attach (from `focus-session`): the read-only screen, with no input bar or key strip |

## What this does not show

- **A real phone.** The keyboard is faked by shrinking `visualViewport.height`,
  as iOS does, and the drags are CDP touch events. The guide's real-phone
  checklist covers iPhone Safari and Android Chrome.
- **Real tmux.** The stub writes a fixed screen. The server half (history
  prefill, line-feed scrolling, restoring the window size) is tested against
  real tmux in `tests/test_terminal_pty.py`.
