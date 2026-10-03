"""§5.3's choice buttons on Discord: render them, and honour a press.

An escalation that carries ``choices`` is answerable from the phone (§5.3):
its one post grows a button per choice plus ``Reply…``, and a press from an
allow-listed user records a verified human reply exactly as a thread reply
does, which moves the post to §5.2's *answered* form and wakes the supervisor.

Two rules decide everything here, and both are the spec's rather than ours:

* **Only allow-listed presses count** (§2.1, restated for buttons).  The
  check is the same one :mod:`src.discord.escalation_intake` applies to thread
  replies -- the gateway's authenticated account id against
  ``discord.authorized_users`` -- so a stranger's tap records nothing, replies
  nothing anybody else can see, and leaves the incident exactly as it was.  The
  one thing a stranger *does* get is the ephemeral "not authorized" §5.3 asks
  for, because Discord shows an error spinner to anyone who is not answered.
* **A press names an option, it does not carry one** (§5.3's evidence rule).
  The custom id names the incident and an index
  (:mod:`src.escalations.interactions`); the option's *text* is read back out
  of the incident's durable row before anything is recorded.  A payload naming
  an index the row does not have, or arriving on a post this incident is not
  bound to, is refused.

The view is built, sent, and then stopped.  That is deliberate: discord.py keeps
a sent view in an in-memory store keyed by message id, and a press would only
be dispatched there while the daemon that sent it is alive.  Escalation posts
outlive restarts, so the press is handled by
:meth:`~src.discord.bot.AgentQueueBot.on_interaction` instead -- one entry
point, identical behaviour for a post sent a minute ago and one sent last week.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import discord

from src.commands.principal import ExecutionPrincipal, principal_context
from src.discord.render_budget import cut
from src.escalations.facts import TERMINAL_STATES
from src.escalations.interactions import (
    KIND_REPLY,
    ButtonSpec,
    choice_text,
    parse_custom_id,
)

logger = logging.getLogger(__name__)

#: The one INFO line per ignored press: code and ids, never the choice text.
BUTTON_IGNORE_LOG_FORMAT = (
    "discord button ignored reason=%s message=%s user=%s button=%s"
)

#: Discord rejects an interaction that is not answered inside three seconds, so
#: every confirmation is cut well inside one read of a phone screen.
MAX_ACK_CHARS = 200

#: Outcome codes :class:`EscalationButtonPress` returns.  They are stable: they
#: are what the log line carries and what a test asserts.
OUTCOME_RECORDED = "recorded"
OUTCOME_REPLAYED = "replayed"
OUTCOME_REFUSED = "refused"
NOT_OUR_BUTTON = "not_an_escalation_button"
NOT_AUTHORIZED = "not_authorized"
UNKNOWN_INCIDENT = "unknown_incident"
CLOSED_INCIDENT = "closed_incident"
NO_CHOICE = "no_choice"
UNBOUND_POST = "unbound_post"
NO_MESSAGE = "no_message"
REPLY_GUIDE = "reply_guide"

_NOT_AUTHORIZED_TEXT = "You are not on this channel's allow-list, so this was not recorded."
_UNKNOWN_INCIDENT_TEXT = "That escalation is no longer here, so nothing was recorded."
_CLOSED_TEXT = (
    "This one is already closed, so nothing was recorded. Reply in the thread if a new "
    "question comes up."
)


def _button_style(spec: ButtonSpec) -> discord.ButtonStyle:
    """Choices are the answer, so they read as primary; ``Reply…`` does not."""
    if spec.kind == KIND_REPLY:
        return discord.ButtonStyle.secondary
    return discord.ButtonStyle.primary


def build_view(specs: Sequence[ButtonSpec]) -> discord.ui.View | None:
    """The components for one escalation post, or ``None`` when it has none.

    ``timeout=None`` is what makes the view persistent, which Discord requires
    of any view that carries custom ids.  The caller must :meth:`stop` it once
    the write is confirmed: the components live on the message, and the
    dispatcher that would otherwise own the press is this module's caller.
    """
    if not specs:
        return None
    view = discord.ui.View(timeout=None)
    for spec in specs:
        view.add_item(
            discord.ui.Button(
                style=_button_style(spec),
                label=spec.label,
                custom_id=spec.custom_id,
            )
        )
    return view


class EscalationButtonPress:
    """Turn one button press into a verified reply, or into nothing at all."""

    def __init__(
        self,
        handler: Any,
        config: Any,
        *,
        reconcile: Callable[[str], Any] | None = None,
        on_ignore: Callable[[str], None] | None = None,
    ) -> None:
        self._handler = handler
        self._config = config
        self._reconcile = reconcile
        self._on_ignore = on_ignore

    @property
    def _authorized_users(self) -> tuple[str, ...]:
        return tuple(str(user).strip() for user in self._config.discord.authorized_users)

    def _ignore(self, code: str, interaction: Any, custom_id: str) -> None:
        logger.info(
            BUTTON_IGNORE_LOG_FORMAT,
            code,
            getattr(getattr(interaction, "message", None), "id", None),
            getattr(getattr(interaction, "user", None), "id", None),
            custom_id,
        )
        if self._on_ignore is not None:
            try:
                self._on_ignore(code)
            except Exception:
                logger.debug("button ignore hook failed", exc_info=True)

    async def _respond(self, interaction: Any, text: str) -> None:
        """Answer the interaction ephemerally: only the presser ever sees this.

        A press is a private act -- somebody tapping a button on their own phone
        -- so every confirmation is ephemeral, and an already-consumed response
        degrades to a follow-up rather than raising into the gateway.
        """
        message = cut(text, MAX_ACK_CHARS)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logger.debug("could not answer an escalation button press", exc_info=True)

    async def handle(self, interaction: Any) -> str:
        """Handle one component interaction; the outcome code is what happened.

        Every refusal is silent in the channel and loud in the log, the same
        contract :mod:`src.discord.escalation_intake` keeps for thread replies.
        """
        custom_id = str((getattr(interaction, "data", None) or {}).get("custom_id") or "")
        press = parse_custom_id(custom_id)
        if press is None:
            # Not one of ours.  Nothing is logged about it and nothing is
            # answered: another feature's button owns this interaction, and the
            # bot hands it straight back.
            return NOT_OUR_BUTTON

        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        if not user_id or user_id not in self._authorized_users:
            self._ignore(NOT_AUTHORIZED, interaction, custom_id)
            await self._respond(interaction, _NOT_AUTHORIZED_TEXT)
            return OUTCOME_REFUSED

        incident = await self._handler.db.get_escalation(press.escalation_id)
        if incident is None:
            self._ignore(UNKNOWN_INCIDENT, interaction, custom_id)
            await self._respond(interaction, _UNKNOWN_INCIDENT_TEXT)
            return OUTCOME_REFUSED

        bound = await self._unbound_reason(incident["id"], interaction)
        if bound is not None:
            self._ignore(bound, interaction, custom_id)
            await self._respond(interaction, _UNKNOWN_INCIDENT_TEXT)
            return OUTCOME_REFUSED

        if str(incident.get("state") or "") in TERMINAL_STATES:
            self._ignore(CLOSED_INCIDENT, interaction, custom_id)
            await self._respond(interaction, _CLOSED_TEXT)
            return OUTCOME_REFUSED

        if press.is_reply:
            await self._respond(
                interaction,
                "Reply in this incident's thread (or on its page) and the supervisor "
                "reads it the same way.",
            )
            return REPLY_GUIDE

        # The option's text comes from the row, never from the payload.
        choices = incident.get("choices") or ()
        if isinstance(choices, dict):  # tolerate a {"a": "..."} shaped column
            choices = tuple(choices.values())
        elif isinstance(choices, str):
            choices = (choices,)
        text = choice_text(tuple(str(item) for item in choices), press)
        if not text:
            self._ignore(NO_CHOICE, interaction, custom_id)
            await self._respond(
                interaction,
                "Those options have changed since this post was written, so nothing was recorded.",
            )
            return OUTCOME_REFUSED

        result = await self._record(
            incident,
            interaction=interaction,
            user_id=user_id,
            text=text,
        )
        if isinstance(result, dict) and result.get("error"):
            code = result.get("error_code") or "reply_failed"
            self._ignore(f"refused:{code}", interaction, custom_id)
            await self._respond(interaction, f"Not recorded: {result['error']}")
            return OUTCOME_REFUSED
        created = bool(isinstance(result, dict) and result.get("created"))
        await self._respond(
            interaction,
            f"Noted: {text}. The {incident['project_id']} supervisor is acting on it.",
        )
        if created and self._reconcile is not None:
            # The acknowledgement is a planned delivery: reconciling now only
            # makes it prompt, and the orchestrator's own tick picks it up
            # regardless.  A failure here is invisible to the human.
            try:
                await self._reconcile(str(incident["id"]))
            except Exception:
                logger.debug("post-press reconcile failed; the cycle will retry", exc_info=True)
        return OUTCOME_RECORDED if created else OUTCOME_REPLAYED

    async def _unbound_reason(self, escalation_id: str, interaction: Any) -> str | None:
        """Why this press did not come from the post this incident is bound to.

        The allow-list says who may answer; this says the button was really
        ours.  Discord sends the message a press came from, and the incident's
        durable delivery rows name the message its root lives in, so a payload
        replayed somewhere else -- or a post whose root was replaced -- cannot
        answer for the incident it names.  ``None`` means it checks out.
        """
        message = getattr(interaction, "message", None)
        if message is None:
            return NO_MESSAGE
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return NO_MESSAGE
        rows = await self._handler.db.list_escalation_deliveries(escalation_id)
        if any(str(row.get("root_message_id") or "") == message_id for row in rows):
            return None
        return UNBOUND_POST

    async def _record(
        self, incident: dict[str, Any], *, interaction: Any, user_id: str, text: str
    ) -> dict[str, Any]:
        """Persist the press as verified human evidence, exactly as a reply is.

        Same command, same principal shape and therefore the same
        ``human:discord:<id>`` actor :meth:`_verified_reply_identity` derives,
        and an ``external_message_id`` keyed on the interaction id so a gateway
        redelivery collapses onto the same row instead of recording the answer
        twice.
        """
        principal = ExecutionPrincipal.service(f"discord:{user_id}")
        with principal_context(principal):
            return await self._handler.execute(
                "escalation_reply",
                {
                    "escalation_id": incident["id"],
                    "text": text,
                    "external_message_id": f"interaction:{getattr(interaction, 'id', '')}",
                },
            )


__all__ = [
    "BUTTON_IGNORE_LOG_FORMAT",
    "CLOSED_INCIDENT",
    "NOT_AUTHORIZED",
    "NOT_OUR_BUTTON",
    "NO_CHOICE",
    "NO_MESSAGE",
    "OUTCOME_RECORDED",
    "OUTCOME_REFUSED",
    "OUTCOME_REPLAYED",
    "REPLY_GUIDE",
    "UNBOUND_POST",
    "UNKNOWN_INCIDENT",
    "EscalationButtonPress",
    "build_view",
]