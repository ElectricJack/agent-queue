from enum import Enum


class IntelligenceClassReferenceKind(str, Enum):
    AGENT = "agent"
    PROFILE = "profile"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
