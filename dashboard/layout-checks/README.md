# Layout checks

Geometry checks in headless Chrome at the mobile-dashboard spec's viewports
(320×568, 390×844, 844×390, 1440×900, 200% zoom with reduced motion), against
the built bundle and a stub daemon on `127.0.0.1:<ephemeral>`. They assert page
overflow, 44 px primary controls on phones, titles, focus traps, history and
pagination, and that no terminal WebSocket opens where the spec forbids one
(a phone's input-only `/ws/terminal/{id}/input` socket opens only after Type).
Screenshots in `.out/` are evidence, never the assertion.

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
`unhandled`, `terminalUpgrades` (attach attempts, always refused) and
`terminalInputs` (accepted input-only sockets and their frames; `typed(session)`
lists the input frames) and takes `override(key, handler)` and the
pane/event controls in `server.mjs`. Fixtures are `fixtures/<area>.mjs`
exporting `routes`; a key defined twice, or a request with no fixture, fails
the run. `fixtures/pane-frames.json` is the pane stream the stub replays;
`tests/test_pane_stream_api.py` holds its frames to the real route's keys and
value types, so change the fixture, never that test. Mark primary controls
`data-primary-control`; mark an intentional
sideways scroller `data-allow-overflow-x`. The dashboard is not built in
GitHub CI: record this command on your close.
