from enum import Enum


class AgentConfigCodexServiceTierType0(str, Enum):
    DEFAULT = "default"
    FAST = "fast"

    def __str__(self) -> str:
        return str(self.value)
