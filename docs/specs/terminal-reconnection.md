# Dashboard terminal reconnection

Interactive viewers retry unexpected transport failures indefinitely with jittered
exponential backoff from 0.5 s to 15 s. They show `Reconnecting… (attempt N)` and
offer `Reconnect now`. Input is disabled and discarded until the new PTY ready
frame; neither input nor old output acknowledgements cross connection generations.
A fresh tmux attach redraws the pane, with a queued VT reset before its new output.
The renderer and viewer remain mounted through transport recovery.

Retries pause while the document is hidden or the browser is offline. Visibility,
online and notification-stream recovery immediately wake pending retries. A ready
connection must remain stable for 30 s before its backoff resets. Handshake and
application ping/pong deadlines detect connections that silently stop responding.
Pings are transport controls, never terminal input or agent activity.

Exit, explicit viewer close, authorization refusal and protocol/identity errors
stop retrying. An opaque browser handshake failure is diagnosed with a read-only
`GET /ws/terminal/{session_id}` probe, generated into both API clients. The probe
uses the same origin, credentials, loopback and session checks as the WebSocket;
it never attaches a PTY. Its response identifies ready, exited, or error, with a
safe message, close code and retryability. The edge's existing terminal-prefix
gate also covers this HTTP request and admits it from the same explicitly
trusted LAN origins as the terminal WebSocket. Same-origin browser GETs may
omit `Origin`: for this read-only GET only, use the single `browser_origin`
query value, or the request scheme and Host when it is absent. An actual
`Origin` header takes precedence and still must pass the Origin gate. Missing
or duplicate/malformed query origins cannot widen access, and remote bearer
requests remain refused. WebSocket handshake denials stay denials.
Known server errors carry their code and retryability in their control frame.

The proxy has no WebSocket idle receive timeout. Enable upstream ping/pong every
15 s; browser-facing keepalive uses terminal JSON ping/pong every 15 s with a
45 s response deadline, suspended in hidden/offline tabs. Existing credit-based
output backpressure remains bounded. Daemon/dashboard restarts are retryable.

Watch-only pane viewers also own jittered retries, including when EventSource
enters CLOSED after a failed handshake. They retain the last screen, replace it
with the fresh full snapshot, expose retry attempt and a manual action, and stop
on server stopped/error frames or unmount. Existing SSE heartbeat comments remain.

Verification covers indefinite capped retries, environmental recovery, stalled
handshakes/keepalive, refusal and exit, discarded input, stale ACKs and redraw,
component recovery without renderer remount, pane CLOSED recovery, and server
ping/probe security through the real proxy.
