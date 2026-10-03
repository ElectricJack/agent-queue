"""Gateway-observed provenance for daemon-internal conversation intake."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.config import is_discord_snowflake
from src.conversations.intake import DM_GUILD, DM_THREAD_PREFIX, normalise_tag


class ConversationEnvelope(BaseModel):
    """Built by the gateway, never authenticated from an HTTP request body."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    transport: Literal["discord"]
    guild_id: str
    channel_id: str
    external_message_id: str
    external_root_message_id: str
    external_thread_id: str | None = None
    author_id: str
    text: str
    received_at: float
    mentions_bot: bool
    #: What the thread hangs off (chat-extension spec §2.3), as ``<kind>:<id>``
    #: for the kinds a conversation may be about.  ``None`` when durable state
    #: names nothing, which is not an error.
    tag: str | None = None

    @field_validator(
        "guild_id",
        "channel_id",
        "external_message_id",
        "external_root_message_id",
        "external_thread_id",
        "author_id",
    )
    @classmethod
    def snowflake(cls, value: str | None) -> str | None:
        # A direct message has no guild and no Discord thread, so §2.1 admits
        # it under the two explicit sentinels a reader can grep for. Anything
        # else must be the snowflake the gateway reported.
        if isinstance(value, str) and value.startswith(DM_THREAD_PREFIX):
            if not is_discord_snowflake(value.removeprefix(DM_THREAD_PREFIX)):
                raise ValueError("Discord ids must be 17-20 ASCII digits")
            return value
        if value == DM_GUILD:
            return value
        if value is not None and (
            not is_discord_snowflake(value) or not value.isascii() or value != value.strip()
        ):
            raise ValueError("Discord ids must be 17-20 ASCII digits")
        return value

    @field_validator("tag")
    @classmethod
    def thread_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if normalise_tag(value) is None:
            raise ValueError("tag must be <kind>:<id> for a known conversation kind")
        return value

    @model_validator(mode="after")
    def root_matches_top_level(self) -> ConversationEnvelope:
        # A top-level message is its own conversation root. Whether it needs a
        # bot mention is the P2 routing flag's decision (§2.2), made by the
        # gateway router and re-checked at the command boundary, not by the
        # shape of the envelope.
        if self.external_thread_id is None and (
            self.external_root_message_id != self.external_message_id
        ):
            raise ValueError("top-level intake requires the message to be its own root")
        if self.guild_id == DM_GUILD and not str(self.external_thread_id or "").startswith(
            DM_THREAD_PREFIX
        ):
            raise ValueError("a direct message is admitted as the dm: thread of its channel")
        if self.guild_id != DM_GUILD and str(self.external_thread_id or "").startswith(
            DM_THREAD_PREFIX
        ):
            raise ValueError("a dm: thread belongs to a direct message, not a guild channel")
        return self
