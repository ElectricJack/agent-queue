"""Message CLI — ``aq message *``, ``aq reply``, ``aq chat``.

Implements docs/specs/implementation/supervisor-agent.md §6.3 (Phase 1).
Hand-crafted rather than auto-generated so every command routes through
:func:`src.cli.envelope.emit` and shares the versioned JSON envelope from
aq-surface §4 — the auto-generated commands print raw payloads instead.

``aq reply <msg-id> "…"`` is the agent-facing top-level alias for
``aq message reply``: it is the protocol the shipped supervisor profile
teaches, so it has to be one word deep.

``aq chat`` is **poll mode** here.  The live ``/ws/events`` subscription that
renders ``queued → delivered → reply`` transitions belongs to Phase 3, which
needs the delivery engine; until then the REPL polls
``GET /api/sessions/{name}/messages?since=`` and prints the reply when it
lands.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

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
    kind, ident = _split_recipient(to, to_kind, to_id)
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
    exit.  Without it you get a REPL; Ctrl-D ends it (the conversation itself
    lives in the supervisor's session, not in this process).

    Poll mode only for now — live ``queued → delivered`` transitions over
    ``/ws/events`` arrive with the Phase 3 delivery engine.
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

    console.print(f"[dim]chat → {session}  (Ctrl-D to exit)[/]")
    while True:
        try:
            line = console.input("[bold bright_cyan]you ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not line:
            continue

        async def _turn(text: str = line):
            async with _get_client(api_url) as client:
                return await _exchange(
                    client, session, text, thread_id=thread_id, timeout=timeout
                )

        result = _run(_turn())
        reply = result.get("reply")
        if reply:
            console.print(f"[bold bright_magenta]{session} ›[/] {reply.get('body', '')}")
        else:
            console.print(f"[yellow]…no reply within {timeout:.0f}s (message still queued)[/]")
