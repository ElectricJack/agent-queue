# Layout checks

Geometry checks in headless Chrome at the mobile-dashboard spec's viewports
(320×568, 390×844, 844×390, 1440×900, 200% zoom with reduced motion), against
the built bundle and a stub daemon on `127.0.0.1:<ephemeral>`. They assert page
overflow, 44 px primary controls on phones, titles, focus traps, history and
pagination, and that no terminal WebSocket opens where the spec forbids one.
Screenshots in `.out/` are evidence, never the assertion.

The `phone-terminal` check runs at `TERMINAL_PHONES` in `profiles.mjs` (the
three phones plus an Android 412×915): the phone attaches like the desktop and
asks for history and its size back, the screen fills its host, rotation and the
on-screen keyboard resize tmux, a touch drag scrolls the scrollback with
momentum, typing goes over the attach, and its colours match the desktop
terminal's. `focus-session` also covers a refused attach: the same terminal
shows the refusal and keeps input disabled.

The `terminal-parity` check runs at the same four sizes and opens the **host
shell page** and the phone's agent terminal in turn, because they are the same
terminal: it `deepEqual`s their computed palettes (font, foreground, every
background xterm paints and the 16-colour, 256-colour, truecolor and background
samples), asserts neither offers a watch/type mode, checks both attach at a size
fitted to the phone and that a touch drag reaches earlier output on both, then
follows rotation and the on-screen keyboard through the agent terminal. Its
screenshots are the side-by-side evidence in
`docs/reports/mobile-terminal-host-shell-parity-2026-10-09/`.

`check:terminal-parity` runs Playwright against the same isolated production
bundle at iPhone 14, iPhone SE and Android Pixel 7 viewports. It compares the
host shell with the focus session, flock agent, pool instance, full session's
Pane tab and session-peek drawer. It verifies raw keyboard input, rotation,
visualViewport keyboard coverage, fitted PTY rows/columns and touch scrolling
back through all 300 fixture history lines. It writes a JSON report and exact
side-by-side screenshots. Chromium emulation does not replace the guide's
real iOS Safari and Android Chrome checks.

```bash
npm -w dashboard run build
npm -w dashboard run check:terminal-parity -- --out /tmp/terminal-parity
```

The `graph-theme` check verifies the task canvas background, grid and controls
in both themes, including changes to the system theme without a page reload.
It uses the axe-core version bundled with jest-axe in Chrome to measure the
eleven text pairs in graph redesign spec §4.2, plus the six graphics pairs.
Token samples exercise the palette independently of card markup.

The `graph-cards` check renders one card per status, a review wait, an epic
and two boundary stubs, and runs axe over the whole graph region in both
themes. Text on the running cards' stripes is a gradient to axe, so the check
measures it itself against the stripe tint. It also proves the stripes and
the running pulse stop under `prefers-reduced-motion`.

```bash
npm -w dashboard run check:layout -- --only graph-theme,graph-cards
```

```bash
npm -w dashboard run build
npm -w dashboard run check:layout [-- --only focus-task --profiles phone-390]
npm -w dashboard run check:layout:selftest
```

`CHROME` overrides `/usr/bin/google-chrome`. Never point a check at a live
dashboard (on the operator's box 5173 is the live dashboard; 8081/8082 are
daemon ports). A check is `checks/<name>.check.mjs` exporting `name`,
optional `profiles` and `run(t)`, where `t` is `{ page, stub, profile,
isPhone, url(path), sockets, shot(label) }`; `stub` records `requests`,
`unhandled`, `terminalUpgrades` (every attach attempt's URL; refused, and the
access probe answers 4403, unless `allowTerminal(session, screen)` gave the
session a fake PTY) and `terminalViewers` (accepted attaches: `url`, `frames`,
`open`; `typed(session)` lists their input, `terminalWrite(session, text)` is
live output) and takes `override(key, handler)` and the pane/event controls in
`server.mjs`. Fixtures are `fixtures/<area>.mjs`
exporting `routes`; a key defined twice, or a request with no fixture, fails
the run. `fixtures/pane-frames.json` is the pane stream the stub replays;
`tests/test_pane_stream_api.py` holds its frames to the real route's keys and
value types, so change the fixture, never that test. Mark primary controls
`data-primary-control`; mark an intentional
sideways scroller `data-allow-overflow-x`. The dashboard is not built in
GitHub CI: record this command on your close.
