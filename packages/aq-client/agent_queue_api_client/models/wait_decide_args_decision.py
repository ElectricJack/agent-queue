from enum import Enum


class WaitDecideArgsDecision(str, Enum):
    CANCEL = "cancel"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    EXTEND = "extend"
    REROUTE = "reroute"
    WAKE = "wake"

    def __str__(self) -> str:
        return str(self.value)
