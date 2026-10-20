from enum import Enum


class WaitLegacyBridgeArgsMode(str, Enum):
    AGENT_WAITS = "agent-waits"
    PROJECT = "project"

    def __str__(self) -> str:
        return str(self.value)
