"""What must hold before a Discord conversation message is accepted (mention-routing spec §4).

Checked twice — in the gateway router and again in ``supervisor_inbox_post`` —
so both answer from this one function.  Every unmet precondition is named, in
:data:`PRECONDITION_CODES` order, so a status surface can say exactly what an
operator still has to do instead of "conversations are off".

Fail closed: an empty ``discord.authorized_users`` admits everyone at the old
gateway layer (``AgentQueueBot._is_authorized``) but disables conversations here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import AppConfig

PRECONDITION_CODES = (
    "conversation_disabled",
    "empty_allowlist",
    "no_guild",
    "no_channel",
    "messages_disabled",
    "sessions_disabled",
    "cutover_incomplete",
    "outbox_unbound",
)


@dataclass(frozen=True)
class ConversationPreconditions:
    """The unmet precondition codes; empty means conversations may be accepted."""

    unmet: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.unmet

    def to_dict(self) -> dict:
        return {"ok": self.ok, "unmet": list(self.unmet)}


def conversation_preconditions(
    config: AppConfig, *, cutover_status: str | None, outbox_bound: bool
) -> ConversationPreconditions:
    """Name every precondition *config* and the live daemon state leave unmet.

    ``cutover_status`` is the Discord cutover report's status (``None`` while
    it has not run); only ``"complete"`` passes.  ``outbox_bound`` says whether
    the delivery adapter has bound the conversation outbox port.
    """
    discord = config.discord
    unmet: list[str] = []
    if not discord.conversation.enabled:
        unmet.append("conversation_disabled")
    if not discord.has_allowlist:
        unmet.append("empty_allowlist")
    if not discord.guild_id:
        unmet.append("no_guild")
    if not discord.channel_id:
        unmet.append("no_channel")
    if not config.messages.enabled:
        unmet.append("messages_disabled")
    # How the daemon cold-starts ``supervisor-global`` (src/messages/session_lens.py).
    if not config.sessions.enabled:
        unmet.append("sessions_disabled")
    if cutover_status != "complete":
        unmet.append("cutover_incomplete")
    if not outbox_bound:
        unmet.append("outbox_unbound")
    return ConversationPreconditions(tuple(unmet))


__all__ = ["PRECONDITION_CODES", "ConversationPreconditions", "conversation_preconditions"]
