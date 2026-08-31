"""Message CLI — ``aq message *``, ``aq reply``, ``aq chat``.

Implements docs/specs/implementation/supervisor-agent.md §6.3.
Hand-crafted rather than auto-generated so every command routes through
:func:`src.cli.envelope.emit` and shares the versioned JSON envelope from
aq-surface §4 — the auto-generated commands print raw payloads instead.

``aq reply <msg-id> "…"`` is the agent-facing top-level alias for
``aq message reply``: it is the protocol the shipped supervisor profile
teaches, so it has to be one word deep.

``aq chat`` runs a REPL that subscribes to the daemon's ``/ws/events``
stream (see :mod:`src.api.websocket`) and renders ``delivered → replied``
transitions live.  If the WebSocket is unavailable (older daemon, no
``websockets`` package, connection refused), it falls back to polling
``GET /api/sessions/{name}/messages?since=`` — the same code path used by
``aq chat --once`` for scripting.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit

#: Poll interval for `aq chat` while waiting on a reply.
_POLL_INTERVAL = 1.0

#: Matches ``MessagesConfig.reply_timeout`` — the CLI can't read daemon
#: config, so the default is duplicated here and overridable with --timeout.
_DEFAULT_REPLY_TIMEOUT = 120.0

VALID_TO_KINDS = ("session", "task", "profile", "user")
VALID_FROM_KINDS = ("session", "user", "system")


def supervisor_session_name(project_id: str) -> str:
    """Logical session name for a project's supervisor (design §5)."""
    return f"supervisor-{project_id}"


def _split_recipient(to: str | None, to_kind: str | None, to_id: str | None) -> tuple[str, str]:
    """Resolve ``--to KIND:ID`` / ``--to-kind`` + ``--to-id`` into a pair.

    Raises ``click.UsageError`` with a concrete message rather than letting a
    malformed recipient reach the daemon as a validation error.
    """
    if to:
        if ":" not in to:
            raise click.UsageError(
                f"--to must be KIND:ID (kinds: {', '.join(VALID_TO_KINDS)}); got {to!r}. "
                "Use --to-kind/--to-id if the id itself contains a colon."
            )
        kind, _, ident = to.partition(":")
        to_kind = to_kind or kind
        to_id = to_id or ident
    if not to_kind or not to_id:
        raise click.UsageError("a recipient is required: --to KIND:ID (or --to-kind/--to-id)")
    if to_kind not in VALID_TO_KINDS:
        raise click.UsageError(
            f"invalid recipient kind {to_kind!r}; expected one of {', '.join(VALID_TO_KINDS)}"
        )
    return to_kind, to_id


def _resolve_inbox_recipient(
    to: str | None,
    to_kind: str | None,
    to_id: str | None,
    *,
    hook_safe: bool = False,
) -> tuple[str, str] | None:
    """Resolve explicit recipient, then the current session/task identity."""
    import os as _os
    explicit = bool(to or to_kind or to_id)
    if not explicit:
        env_session = _os.environ.get("AQ_SESSION_ID")
        env_task = _os.environ.get("AQ_TASK_ID")
        if env_session:
            to_kind, to_id = "session", env_session
        elif env_task:
            to_kind, to_id = "task", env_task
        elif hook_safe:
            return None
    return _split_recipient(to, to_kind, to_id)


def _render_message_table(rows: list[dict], title: str) -> None:
    from rich.table import Table

    table = Table(title=title, title_style="bold bright_white", border_style="bright_black")
    table.add_column("ID", style="bold bright_cyan", no_wrap=True)
    table.add_column("From")
    table.add_column("To")
    table.add_column("Subject")
    table.add_column("Body")
    table.add_column("State")
    for row in rows:
        if row.get("archived_at"):
            state = "archived"
        elif row.get("read"):
            state = "read"
        elif row.get("delivered"):
            state = "delivered"
        else:
            state = "queued"
        body = (row.get("body") or "").replace("\n", " ")
        table.add_row(
            row.get("id", ""),
            row.get("from", ""),
            row.get("to", ""),
            row.get("subject") or "",
            body[:60] + ("…" if len(body) > 60 else ""),
            state,
        )
    console.print(table)


@cli.group("message")
def message() -> None:
    """Inter-agent and user message queue."""


# ---------------------------------------------------------------------------
# aq message send
# ---------------------------------------------------------------------------


@message.command("send")
@click.option("-p", "--project", "project_id", default=None, help="Owning project id")
@click.option("--to", default=None, help="Recipient as KIND:ID, e.g. session:supervisor-aq")
@click.option("--to-kind", default=None, type=click.Choice(VALID_TO_KINDS), help="Recipient kind")
@click.option("--to-id", default=None, help="Recipient id")
@click.option("-b", "--body", required=True, help="Markdown message body")
@click.option("-s", "--subject", default=None, help="Optional subject line")
@click.option("--from-id", default="cli", help="Sender id (default: cli)")
@click.option(
    "--from-kind",
    default="user",
    type=click.Choice(VALID_FROM_KINDS),
    help="Sender kind (default: user)",
)
@click.option("--thread-id", default=None, help="Conversation grouping key")
@click.option("--priority", default=100, type=int, help="Delivery ordering, lower first")
@click.option(
    "--archive-after-inject",
    is_flag=True,
    default=False,
    help="Archive the row once it is injected into a prompt",
)
@click.option(
    "--pane-open",
    "pane_open_json",
    default=None,
    help=(
        "Attach a pane_open frame: JSON like "
        "'{\"view\": \"task-detail\", \"args\": {\"taskId\": \"t1\"}}'"
    ),
)
@click.pass_context
@_handle_errors
def message_send(
    ctx: click.Context,
    project_id: str | None,
    to: str | None,
    to_kind: str | None,
    to_id: str | None,
    body: str,
    subject: str | None,
    from_id: str,
    from_kind: str,
    thread_id: str | None,
    priority: int,
    archive_after_inject: bool,
    pane_open_json: str | None,
) -> None:
    """Queue a message to a session, task, profile, or user."""
    kind, ident = _split_recipient(to, to_kind, to_id)
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    params: dict[str, Any] = {
        "to_kind": kind,
        "to_id": ident,
        "body": body,
        "from_kind": from_kind,
        "from_id": from_id,
        "priority": priority,
        "archive_after_inject": archive_after_inject,
    }
    if project_id:
        params["project_id"] = project_id
    if subject:
        params["subject"] = subject
    if thread_id:
        params["thread_id"] = thread_id
    if pane_open_json:
        import json as _json

        try:
            params["pane_open"] = _json.loads(pane_open_json)
        except _json.JSONDecodeError as exc:
            click.echo(f"--pane-open: invalid JSON: {exc}", err=True)
            raise click.Abort() from exc

    async def _send():
        async with _get_client(api_url) as client:
            return await client.execute("message_send", params)

    result = _run(_send())

    def _render(data: dict) -> None:
        console.print(
            f"[bold green]Message queued:[/] [bold bright_cyan]{data.get('message_id')}[/] "
            f"→ {kind}:{ident}"
        )

    emit(ctx, result, render=_render)


# ---------------------------------------------------------------------------
# aq message reply  /  aq reply
# ---------------------------------------------------------------------------


def _do_reply(ctx: click.Context, message_id: str, body: str, via: str | None) -> None:
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params: dict[str, Any] = {"message_id": message_id, "body": body}
    if via:
        params["via"] = via

    async def _reply():
        async with _get_client(api_url) as client:
            return await client.execute("message_reply", params)

    result = _run(_reply())

    def _render(data: dict) -> None:
        console.print(
            f"[bold green]Replied to[/] [bold bright_cyan]{data.get('message_id')}[/] "
            f"([dim]reply {data.get('reply_id')}[/])"
        )

    emit(ctx, result, render=_render)


@message.command("reply")
@click.argument("message_id")
@click.argument("body")
@click.option("--via", default=None, help="Delivery marker, e.g. transcript_tail")
@click.pass_context
@_handle_errors
def message_reply(ctx: click.Context, message_id: str, body: str, via: str | None) -> None:
    """Reply to a message by id."""
    _do_reply(ctx, message_id, body, via)


@cli.command("reply")
@click.argument("message_id")
@click.argument("body")
@click.option("--via", default=None, help="Delivery marker, e.g. transcript_tail")
@click.pass_context
@_handle_errors
def reply(ctx: click.Context, message_id: str, body: str, via: str | None) -> None:
    """Reply to a message (alias for `aq message reply`).

    This is the reply protocol agents are taught: `aq reply <msg-id> "…"`.
    """
    _do_reply(ctx, message_id, body, via)


# ---------------------------------------------------------------------------
# aq message inbox
# ---------------------------------------------------------------------------


@message.command("inbox")
@click.option("--to", default=None, help="Recipient as KIND:ID, e.g. session:supervisor-aq")
@click.option("--to-kind", default=None, type=click.Choice(VALID_TO_KINDS), help="Recipient kind")
@click.option("--to-id", default=None, help="Recipient id")
@click.option(
    "--inject",
    is_flag=True,
    default=False,
    help="Mark the returned messages delivered (prompt-boundary injection)",
)
@click.option("--limit", default=None, type=int, help="Max rows")
@click.pass_context
@_handle_errors
def message_inbox(
    ctx: click.Context,
    to: str | None,
    to_kind: str | None,
    to_id: str | None,
    inject: bool,
    limit: int | None,
) -> None:
    """Show a recipient's pending (undelivered) messages."""
    resolved = _resolve_inbox_recipient(to, to_kind, to_id)
    assert resolved is not None
    kind, ident = resolved
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    params: dict[str, Any] = {"to_kind": kind, "to_id": ident, "inject": inject}
    if limit is not None:
        params["limit"] = limit

    async def _inbox():
        async with _get_client(api_url) as client:
            return await client.execute("message_inbox", params)

    result = _run(_inbox())

    def _render(data: dict) -> None:
        items = data.get("messages", [])
        if not items:
            console.print(f"[dim]No pending messages for {kind}:{ident}.[/]")
            return
        _render_message_table(items, f"Inbox — {kind}:{ident}")

    emit(ctx, result, render=_render)


# ---------------------------------------------------------------------------
# aq inbox — top-level alias for the UserPromptSubmit hook
# ---------------------------------------------------------------------------


@cli.command("inbox")
@click.option("--to", default=None, help="Recipient as KIND:ID, e.g. session:supervisor-aq")
@click.option("--to-kind", default=None, type=click.Choice(VALID_TO_KINDS), help="Recipient kind")
@click.option("--to-id", default=None, help="Recipient id")
@click.option(
    "--inject",
    is_flag=True,
    default=False,
    help="Mark the returned messages delivered (prompt-boundary injection)",
)
@click.option("--limit", default=None, type=int, help="Max rows")
@click.pass_context
def inbox(
    ctx: click.Context,
    to: str | None,
    to_kind: str | None,
    to_id: str | None,
    inject: bool,
    limit: int | None,
) -> None:
    """Show pending messages (alias for ``aq message inbox``).

    This is the hook-facing form invoked from the harness's
    ``UserPromptSubmit`` hook (``.aq/hooks/claude.json``).  It is
    **hook-safe by design**: no pending messages, an unresolvable recipient
    (session-scope resolution is Phase S2), or a daemon that is not running
    all exit 0 with no stdout — so the hook never blocks the agent's next
    prompt and never leaks tracebacks into the prompt window.  Use
    ``aq message inbox`` for the interactive form that surfaces errors.
    """
    from .exceptions import CommandError, DaemonNotRunningError

    # Recipient resolution.  Explicit --to / --to-kind+--to-id wins; when
    # neither is given the hook path derives the current session or task
    # identity from the session env (``src/sessions/env.py``); the
    # ``UserPromptSubmit`` hook works without shell-injection tricks.
    # No recipient AND no env → silent no-op (hook safety).
    if not (to or (to_kind and to_id)):
        resolved = _resolve_inbox_recipient(to, to_kind, to_id, hook_safe=True)
        if resolved is None:
            return
        kind, ident = resolved
    else:
        try:
            kind, ident = _split_recipient(to, to_kind, to_id)
        except click.UsageError:
            return

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params: dict[str, Any] = {"to_kind": kind, "to_id": ident, "inject": inject}
    if limit is not None:
        params["limit"] = limit

    async def _inbox():
        async with _get_client(api_url) as client:
            return await client.execute("message_inbox", params)

    try:
        result = _run(_inbox())
    except DaemonNotRunningError:
        return  # daemon down → hook is a no-op, never blocks the prompt
    except CommandError:
        return  # command-level failures also stay silent from the hook
    except Exception:
        return

    if not isinstance(result, dict):
        return
    if result.get("error"):
        return
    items = result.get("messages") or []
    if not items:
        return

    # Render each message using the same envelope the delivery engine uses
    # (``[<id> from <kind>:<id>] <subject>\n<body>``) so the agent sees one
    # shape across nudge, inject, and prime.  Plain print (not console) so
    # nothing Rich-formats when the hook is piped into the model context.
    lines: list[str] = []
    for item in items:
        header = f"[{item.get('id', '?')} from {item.get('from', '?')}]"
        subject = item.get("subject") or ""
        if subject:
            header = f"{header} {subject}"
        lines.append(f"{header}\n{item.get('body', '')}")
    click.echo("\n\n".join(lines))


# ---------------------------------------------------------------------------
# aq message list
# ---------------------------------------------------------------------------


@message.command("list")
@click.option("-p", "--project", "project_id", default=None, help="Filter by project")
@click.option("--thread-id", default=None, help="Filter by conversation thread")
@click.option("--to-kind", default=None, type=click.Choice(VALID_TO_KINDS), help="Recipient kind")
@click.option("--to-id", default=None, help="Recipient id")
@click.option("--since", default=None, type=float, help="Only messages after this epoch time")
@click.option("--include-archived", is_flag=True, default=False, help="Include archived rows")
@click.option("--limit", default=100, type=int, help="Max rows")
@click.pass_context
@_handle_errors
def message_list(
    ctx: click.Context,
    project_id: str | None,
    thread_id: str | None,
    to_kind: str | None,
    to_id: str | None,
    since: float | None,
    include_archived: bool,
    limit: int,
) -> None:
    """List messages, newest first."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    params: dict[str, Any] = {"limit": limit, "include_archived": include_archived}
    if project_id:
        params["project_id"] = project_id
    if thread_id:
        params["thread_id"] = thread_id
    if to_kind:
        params["to_kind"] = to_kind
    if to_id:
        params["to_id"] = to_id
    if since is not None:
        params["since"] = since

    async def _list():
        async with _get_client(api_url) as client:
            return await client.execute("message_list", params)

    result = _run(_list())

    def _render(data: dict) -> None:
        items = data.get("messages", [])
        if not items:
            console.print("[dim]No messages.[/]")
            return
        _render_message_table(items, "Messages")

    emit(ctx, result, render=_render)


# ---------------------------------------------------------------------------
# aq chat
# ---------------------------------------------------------------------------


async def _exchange(
    client,
    session: str,
    text: str,
    *,
    thread_id: str | None,
    timeout: float,
    poll_interval: float = _POLL_INTERVAL,
) -> dict:
    """Send one message and poll until its reply arrives or *timeout* elapses.

    Returns ``{"session", "message_id", "state", "reply"}`` where ``state`` is
    ``"replied"`` or ``"timeout"``.  A reply is any message whose
    ``reply_to_id`` matches the one we just sent — the deterministic link the
    reply protocol writes (design §6.2), not a heuristic on authorship.
    """
    since = time.time()
    sent = await client.send_session_message(
        session, text, from_id="cli", thread_id=thread_id
    )
    message_id = sent.get("message_id")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = await client.get_session_messages(session, thread_id=thread_id, since=since)
        for row in page.get("messages", []):
            if row.get("reply_to_id") == message_id:
                return {
                    "session": session,
                    "message_id": message_id,
                    "state": "replied",
                    "reply": row,
                }
        await asyncio.sleep(poll_interval)

    return {
        "session": session,
        "message_id": message_id,
        "state": "timeout",
        "reply": None,
    }


def _events_ws_url(base_url: str) -> str:
    """Translate ``http(s)://host:port`` → ``ws(s)://host:port/ws/events``."""
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/ws/events", "", "", ""))


async def _stream_replies_ws(
    ws_url: str,
    message_id: str,
    session: str,
    *,
    timeout: float,
) -> AsyncIterator[dict]:
    """Yield ``message.*`` frames from ``/ws/events`` until reply/timeout.

    Yields ``{"state": "delivered"|"replied", "reply": dict | None}``.
    Raises :class:`ConnectionError` on any transport failure so the caller
    can fall back to polling.  ``websockets`` is a starlette dep — if the
    import fails at runtime we treat it as unavailable, not a bug.
    """
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - websockets ships transitively
        raise ConnectionError("websockets package unavailable") from exc

    try:
        connection = await websockets.connect(ws_url)
    except (OSError, Exception) as exc:  # noqa: BLE001 — any connect failure → fallback
        raise ConnectionError(f"cannot open {ws_url}: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            except Exception as exc:  # noqa: BLE001
                raise ConnectionError(f"ws recv failed: {exc}") from exc

            import json as _json

            try:
                frame = _json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
            except Exception:
                continue
            event_type = frame.get("_event_type", "")
            if not event_type.startswith("message."):
                continue
            # message.delivered carries the outbound message_id we sent.
            if event_type == "message.delivered" and frame.get("message_id") == message_id:
                yield {"state": "delivered", "reply": None}
                continue
            # message.replied's ``message_id`` is the *original* — the one
            # we sent — and ``reply_id`` is the new row.
            if event_type == "message.replied" and frame.get("message_id") == message_id:
                yield {
                    "state": "replied",
                    "reply": {
                        "id": frame.get("reply_id"),
                        "body": frame.get("body", ""),
                        "reply_to_id": message_id,
                    },
                }
                return
    finally:
        try:
            await connection.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


async def _stream_replies_poll(
    client,
    session: str,
    message_id: str,
    *,
    thread_id: str | None,
    timeout: float,
    poll_interval: float,
    since: float,
) -> AsyncIterator[dict]:
    """Polling fallback: same yield contract as the WS streamer.

    Only emits ``replied`` — the poll path can't observe the delivery
    transition (there is no separate ``delivered_at`` column exposed in
    ``list_messages``), so it goes straight from silence to the reply.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = await client.get_session_messages(session, thread_id=thread_id, since=since)
        for row in page.get("messages", []):
            if row.get("reply_to_id") == message_id:
                yield {"state": "replied", "reply": row}
                return
        await asyncio.sleep(poll_interval)


async def _stream_replies(
    client,
    base_url: str | None,
    session: str,
    message_id: str,
    *,
    thread_id: str | None,
    timeout: float,
    poll_interval: float = _POLL_INTERVAL,
    since: float,
) -> AsyncIterator[dict]:
    """Prefer ``/ws/events``; fall back to polling on any transport error.

    Factored out (and module-level) so tests can patch it without
    exercising a real WebSocket.
    """
    ws_url = _events_ws_url(base_url or "http://127.0.0.1:8081")
    try:
        async for event in _stream_replies_ws(
            ws_url, message_id, session, timeout=timeout
        ):
            yield event
        return
    except ConnectionError:
        pass  # fall through to poll

    async for event in _stream_replies_poll(
        client,
        session,
        message_id,
        thread_id=thread_id,
        timeout=timeout,
        poll_interval=poll_interval,
        since=since,
    ):
        yield event


async def _live_exchange(
    client,
    base_url: str | None,
    session: str,
    text: str,
    *,
    thread_id: str | None,
    timeout: float,
    poll_interval: float = _POLL_INTERVAL,
    on_delivered=None,
) -> dict:
    """Send one line and stream events until the reply lands or times out.

    ``on_delivered`` is an optional sync callback invoked when the
    ``message.delivered`` frame arrives — REPL uses it to render a
    ``delivered`` breadcrumb; ``--once`` doesn't care.
    """
    since = time.time()
    sent = await client.send_session_message(
        session, text, from_id="cli", thread_id=thread_id
    )
    message_id = sent.get("message_id")

    async for event in _stream_replies(
        client,
        base_url,
        session,
        message_id,
        thread_id=thread_id,
        timeout=timeout,
        poll_interval=poll_interval,
        since=since,
    ):
        state = event.get("state")
        if state == "delivered" and on_delivered is not None:
            try:
                on_delivered()
            except Exception:  # pragma: no cover - cosmetic renderer
                pass
            continue
        if state == "replied":
            return {
                "session": session,
                "message_id": message_id,
                "state": "replied",
                "reply": event.get("reply"),
            }
    return {
        "session": session,
        "message_id": message_id,
        "state": "timeout",
        "reply": None,
    }


@cli.command("chat")
@click.argument("project", required=False, default=None)
@click.option("--once", "once", default=None, help="Send one message, print the reply, exit")
@click.option("--thread-id", default=None, help="Conversation grouping key")
@click.option(
    "--timeout",
    default=_DEFAULT_REPLY_TIMEOUT,
    type=float,
    help="Seconds to wait for a reply before giving up",
)
@click.pass_context
@_handle_errors
def chat(
    ctx: click.Context,
    project: str | None,
    once: str | None,
    thread_id: str | None,
    timeout: float,
) -> None:
    """Talk to a project's supervisor session.

    ``--once TEXT`` is the scripting form: send, wait for the reply, print it,
    exit.  Without it you get a REPL that subscribes to ``/ws/events`` and
    renders ``delivered → replied`` transitions live; Ctrl-C or Ctrl-D ends it
    (the conversation itself lives in the supervisor's session, not here).
    """
    if not project:
        raise click.UsageError("a project is required: aq chat <project>")
    session = supervisor_session_name(project)
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if once is not None:

        async def _once():
            async with _get_client(api_url) as client:
                return await _exchange(
                    client, session, once, thread_id=thread_id, timeout=timeout
                )

        result = _run(_once())

        def _render(data: dict) -> None:
            reply = data.get("reply")
            if reply:
                console.print(reply.get("body", ""))
            else:
                console.print(
                    f"[yellow]No reply within {timeout:.0f}s[/] "
                    f"([dim]message {data.get('message_id')} still queued[/])"
                )

        emit(ctx, result, render=_render)
        if result.get("state") != "replied":
            raise SystemExit(1)
        return

    console.print(f"[dim]chat → {session}  (Ctrl-C or Ctrl-D to exit)[/]")
    while True:
        try:
            line = console.input("[bold bright_cyan]you ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not line:
            continue

        def _delivered_note() -> None:
            console.print("[dim]  ·delivered[/]")

        async def _turn(text: str = line):
            async with _get_client(api_url) as client:
                return await _live_exchange(
                    client,
                    api_url,
                    session,
                    text,
                    thread_id=thread_id,
                    timeout=timeout,
                    on_delivered=_delivered_note,
                )

        try:
            result = _run(_turn())
        except KeyboardInterrupt:
            console.print()
            return
        reply = result.get("reply")
        if reply:
            console.print(f"[bold bright_magenta]{session} ›[/] {reply.get('body', '')}")
        else:
            console.print(f"[yellow]…no reply within {timeout:.0f}s (message still queued)[/]")
