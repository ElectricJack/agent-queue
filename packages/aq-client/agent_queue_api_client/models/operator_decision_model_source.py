from enum import Enum


class OperatorDecisionModelSource(str, Enum):
    CHAT = "chat"
    CLI = "cli"
    DISCORD = "discord"

    def __str__(self) -> str:
        return str(self.value)
