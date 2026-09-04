from enum import Enum


class PlaybookV2ImportResponseScope(str, Enum):
    AGENT_TYPE = "agent_type"
    PROJECT = "project"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
