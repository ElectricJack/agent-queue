"""Telegram inline keyboard views — interactive button layouts for task actions.

Mirrors the Discord View classes in ``src/discord/notifications.py`` using
Telegram's InlineKeyboardMarkup.  Each view builder returns a tuple of
``(text, reply_markup)`` suitable for ``bot.send_message()``.

Callback data format: ``"action:key=value,key2=value2"``
The bot's ``_handle_callback_query`` parses this into a command name and args
dict, then routes to ``CommandHandler.execute()``.

View mapping from Discord:

| Discord View            | Telegram Builder                      |
|------------------------|---------------------------------------|
| ``TaskStartedView``    | ``task_started_keyboard()``           |
| ``TaskFailedView``     | ``task_failed_keyboard()``            |
| ``TaskApprovalView``   | ``task_approval_keyboard()``          |
| ``TaskBlockedView``    | ``task_blocked_keyboard()``           |
| ``AgentQuestionView``  | ``agent_question_keyboard()``         |
| ``PlanApprovalView``   | ``plan_approval_keyboard()``          |
"""

from __future__ import annotations

from typing import Any

# Lazy imports — telegram may not be installed
_InlineKeyboardButton = None
_InlineKeyboardMarkup = None


def _ensure_imports() -> None:
    """Lazy-import telegram types on first use."""
    global _InlineKeyboardButton, _InlineKeyboardMarkup
    if _InlineKeyboardButton is None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        _InlineKeyboardButton = InlineKeyboardButton
        _InlineKeyboardMarkup = InlineKeyboardMarkup


def _make_callback_data(action: str, **kwargs: str) -> str:
    """Build a callback_data string from an action name and keyword args.

    Format: ``"action:key=value,key2=value2"``

    Telegram limits callback_data to 64 bytes, so keep args short.
    """
    if not kwargs:
        return action
    pairs = ",".join(f"{k}={v}" for k, v in kwargs.items())
    data = f"{action}:{pairs}"
    if len(data.encode("utf-8")) > 64:
        # Truncate task_id if necessary to fit within 64-byte limit
        # This is a safety measure — callers should use short IDs
        data = data[:64]
    return data


def parse_callback_data(data: str) -> tuple[str, dict[str, str]]:
    """Parse callback_data back into (action, args_dict).

    This is the inverse of ``_make_callback_data``.  Used by the bot's
    callback query handler.

    Returns
    -------
    tuple[str, dict[str, str]]
        ``(action_name, {"key": "value", ...})``
    """
    if ":" not in data:
        return data, {}
    action, rest = data.split(":", 1)
    args: dict[str, str] = {}
    for pair in rest.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            args[k] = v
    return action, args


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def task_started_keyboard(task_id: str) -> Any:
    """Inline keyboard for a task-started notification.

    Buttons: View Context | Stop Task

    Mirrors ``TaskStartedView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\U0001f4cb View Context",
                    callback_data=_make_callback_data("view_context", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\u23f9 Stop Task",
                    callback_data=_make_callback_data("stop_task", task_id=task_id),
                ),
            ],
        ]
    )


def task_failed_keyboard(task_id: str) -> Any:
    """Inline keyboard for a task-failed notification.

    Buttons: Retry | Skip | View Error

    Mirrors ``TaskFailedView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\U0001f504 Retry",
                    callback_data=_make_callback_data("restart_task", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\u23ed Skip",
                    callback_data=_make_callback_data("skip_task", task_id=task_id),
                ),
            ],
            [
                _InlineKeyboardButton(
                    text="\U0001f50d View Error",
                    callback_data=_make_callback_data("get_agent_error", task_id=task_id),
                ),
            ],
        ]
    )


def task_approval_keyboard(task_id: str) -> Any:
    """Inline keyboard for a task-approval notification.

    Buttons: Approve | Restart

    Mirrors ``TaskApprovalView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\u2705 Approve",
                    callback_data=_make_callback_data("approve_task", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\U0001f504 Restart",
                    callback_data=_make_callback_data("restart_task", task_id=task_id),
                ),
            ],
        ]
    )


def task_blocked_keyboard(task_id: str) -> Any:
    """Inline keyboard for a task-blocked notification.

    Buttons: Restart | Skip

    Mirrors ``TaskBlockedView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\U0001f504 Restart",
                    callback_data=_make_callback_data("restart_task", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\u23ed Skip",
                    callback_data=_make_callback_data("skip_task", task_id=task_id),
                ),
            ],
        ]
    )


def agent_question_keyboard(task_id: str) -> Any:
    """Inline keyboard for an agent-question notification.

    Buttons: Reply (prompts user to reply to the message) | Skip

    On Telegram, the "Reply" button tells the user to use Telegram's native
    reply-to-message feature to answer the question.  The bot watches for
    replies to the question message and forwards them to the agent.

    Mirrors ``AgentQuestionView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\U0001f4ac Reply",
                    callback_data=_make_callback_data("agent_reply_prompt", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\u23ed Skip",
                    callback_data=_make_callback_data("skip_task", task_id=task_id),
                ),
            ],
        ]
    )


def plan_approval_keyboard(task_id: str) -> Any:
    """Inline keyboard for a plan-approval notification.

    Buttons: Approve Plan | Delete Plan

    Mirrors ``PlanApprovalView`` from Discord.
    """
    _ensure_imports()
    return _InlineKeyboardMarkup(
        [
            [
                _InlineKeyboardButton(
                    text="\u2705 Approve Plan",
                    callback_data=_make_callback_data("approve_plan", task_id=task_id),
                ),
                _InlineKeyboardButton(
                    text="\U0001f5d1 Delete Plan",
                    callback_data=_make_callback_data("delete_plan", task_id=task_id),
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Keyboard for RichNotification actions
# ---------------------------------------------------------------------------


def notification_actions_keyboard(actions: list[Any]) -> Any | None:
    """Convert a list of ``NotificationAction`` objects to an inline keyboard.

    Each action becomes a button.  Actions are laid out in rows of up to 3.

    Parameters
    ----------
    actions:
        List of ``NotificationAction`` dataclass instances with ``label``,
        ``action_id``, and optional ``args`` dict.
    """
    if not actions:
        return None

    _ensure_imports()

    buttons: list[list[Any]] = []
    row: list[Any] = []
    for action in actions:
        label = getattr(action, "label", str(action))
        action_id = getattr(action, "action_id", str(action))
        args = getattr(action, "args", None) or {}
        callback_data = _make_callback_data(action_id, **{k: str(v) for k, v in args.items()})
        row.append(_InlineKeyboardButton(text=label, callback_data=callback_data))
        if len(row) >= 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    return _InlineKeyboardMarkup(buttons) if buttons else None


# ---------------------------------------------------------------------------
# Utility: update message to show action was taken
# ---------------------------------------------------------------------------


async def disable_keyboard_after_action(
    query: Any,
    result_text: str,
) -> None:
    """Edit the original message to remove the inline keyboard and append a status line.

    Called after a callback query button is pressed to indicate the action
    was completed (mimics Discord's pattern of disabling buttons after use).

    Parameters
    ----------
    query:
        The ``telegram.CallbackQuery`` object from the update.
    result_text:
        Short text to append (e.g. "Task restarted" or "Error: not found").
    """
    try:
        original_text = query.message.text or ""
        updated = f"{original_text}\n\n\u2014 {result_text}"
        await query.edit_message_text(text=updated, reply_markup=None)
    except Exception:
        # Fallback: just remove the keyboard
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
