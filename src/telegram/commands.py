"""Telegram command handlers — thin routing layer to CommandHandler.

Each handler extracts arguments from the Telegram ``Update`` and delegates
to ``CommandHandler.execute(name, args)``.  This mirrors the pattern in
``src/discord/commands.py`` — all business logic lives in CommandHandler,
these are just the Telegram-specific wrappers.

Commands are registered on the ``telegram.ext.Application`` via
``add_handler(CommandHandler(...))``.  The ``register_commands`` function
wires all of them at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.telegram.notifications import (
    bold,
    escape_markdown,
    split_message,
)

if TYPE_CHECKING:
    from src.command_handler import CommandHandler

logger = logging.getLogger(__name__)


async def _send_result(
    update,  # telegram.Update
    result: dict,
    success_title: str = "Success",
) -> None:
    """Send a command result back to the Telegram chat.

    If the result contains an ``"error"`` key, sends an error message.
    Otherwise formats a success response.
    """
    if not update.effective_message:
        return

    if "error" in result:
        text = f"{bold('Error')} {escape_markdown(result['error'])}"
    else:
        # Build a readable response from the result dict
        lines = [bold(success_title)]
        for key, value in result.items():
            if key.startswith("_"):
                continue
            lines.append(f"{bold(key)}: {escape_markdown(str(value))}")
        text = "\n".join(lines)

    for chunk in split_message(text):
        await update.effective_message.reply_text(chunk, parse_mode="MarkdownV2")


async def cmd_create_task(update, context, handler: "CommandHandler") -> None:
    """Handle /create_task <description>."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /create\\_task <description>", parse_mode="MarkdownV2"
        )
        return

    description = " ".join(context.args)
    result = await handler.execute("create_task", {"description": description})
    await _send_result(update, result, "Task Created")


async def cmd_list_tasks(update, context, handler: "CommandHandler") -> None:
    """Handle /list_tasks [status]."""
    args: dict = {}
    if context.args:
        args["status"] = context.args[0]
    result = await handler.execute("list_tasks", args)
    await _send_result(update, result, "Tasks")


async def cmd_status(update, context, handler: "CommandHandler") -> None:
    """Handle /status — show system status."""
    result = await handler.execute("status", {})
    await _send_result(update, result, "Status")


async def cmd_cancel_task(update, context, handler: "CommandHandler") -> None:
    """Handle /cancel_task <task_id>."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /cancel\\_task <task\\_id>", parse_mode="MarkdownV2"
        )
        return
    result = await handler.execute("cancel_task", {"task_id": context.args[0]})
    await _send_result(update, result, "Task Cancelled")


async def cmd_retry_task(update, context, handler: "CommandHandler") -> None:
    """Handle /retry_task <task_id>."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /retry\\_task <task\\_id>", parse_mode="MarkdownV2"
        )
        return
    result = await handler.execute("retry_task", {"task_id": context.args[0]})
    await _send_result(update, result, "Task Retried")


async def cmd_approve_task(update, context, handler: "CommandHandler") -> None:
    """Handle /approve_task <task_id>."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /approve\\_task <task\\_id>", parse_mode="MarkdownV2"
        )
        return
    result = await handler.execute("approve_task", {"task_id": context.args[0]})
    await _send_result(update, result, "Task Approved")


async def cmd_skip_task(update, context, handler: "CommandHandler") -> None:
    """Handle /skip_task <task_id>."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /skip\\_task <task\\_id>", parse_mode="MarkdownV2"
        )
        return
    result = await handler.execute("skip_task", {"task_id": context.args[0]})
    await _send_result(update, result, "Task Skipped")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# Command name -> (handler_function, description) for Telegram's command menu
COMMAND_MAP: dict[str, tuple] = {
    "create_task": (cmd_create_task, "Create a new task"),
    "list_tasks": (cmd_list_tasks, "List tasks (optional: status filter)"),
    "status": (cmd_status, "Show system status"),
    "cancel_task": (cmd_cancel_task, "Cancel a task"),
    "retry_task": (cmd_retry_task, "Retry a failed task"),
    "approve_task": (cmd_approve_task, "Approve a task awaiting approval"),
    "skip_task": (cmd_skip_task, "Skip a task"),
}


def register_commands(application, handler: "CommandHandler") -> None:
    """Register all Telegram command handlers on the Application.

    Parameters
    ----------
    application:
        ``telegram.ext.Application`` instance (from python-telegram-bot).
    handler:
        The ``CommandHandler`` instance that executes business logic.
    """
    from telegram.ext import CommandHandler as TgCommandHandler

    for cmd_name, (func, _description) in COMMAND_MAP.items():
        # Wrap the handler to inject our CommandHandler instance
        async def make_handler(f=func, h=handler):
            async def wrapped(update, context):
                await f(update, context, h)

            return wrapped

        # python-telegram-bot expects (update, context) signature
        async def handler_wrapper(update, context, _f=func, _h=handler):
            await _f(update, context, _h)

        application.add_handler(TgCommandHandler(cmd_name, handler_wrapper))
