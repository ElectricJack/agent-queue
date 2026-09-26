"""Gateway-observed provenance for daemon-internal conversation intake."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.config import is_discord_snowflake


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
        if value is not None and (
            not is_discord_snowflake(value) or not value.isascii() or value != value.strip()
        ):
            raise ValueError("Discord ids must be 17-20 ASCII digits")
        return value

    @model_validator(mode="after")
    def root_matches_top_level(self) -> ConversationEnvelope:
        if self.external_thread_id is None and (
            self.external_root_message_id != self.external_message_id or not self.mentions_bot
        ):
            raise ValueError("top-level intake requires a bot mention on the root message")
        return self
