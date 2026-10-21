"""A gateway envelope for persistent opaque ask controls, including after restart."""

from src.commands.principal import ExecutionPrincipal, principal_context


class AskButtonPress:
    def __init__(self, handler):
        self.handler = handler

    async def handle(self, interaction):
        custom_id = (getattr(interaction, "data", None) or {}).get("custom_id", "")
        if not isinstance(custom_id, str) or not custom_id.startswith("aqask:"):
            return None
        # Acknowledge before potentially contending on a source row lock.
        await interaction.response.defer(ephemeral=True)
        with principal_context(ExecutionPrincipal.service("discord-gateway")):
            result = await self.handler.execute(
                "ask_button",
                {
                    "custom_id": custom_id,
                    "interaction_id": str(interaction.id),
                    "guild_id": str(interaction.guild_id),
                    "channel_id": str(interaction.channel_id),
                    "message_id": str(getattr(interaction.message, "id", "")),
                    "author_id": str(getattr(interaction.user, "id", "")),
                },
            )
        receipt = result.get("receipt", {})
        text = (
            f"Decision {receipt.get('decision_id')}: {receipt.get('application_state', 'recorded')}."
            if result.get("success")
            else result.get("error", "This control is unavailable.")
        )
        await interaction.followup.send(text, ephemeral=True)
        return result
