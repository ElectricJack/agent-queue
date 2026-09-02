from enum import Enum


class NodeBadgeDTOKind(str, Enum):
    BUDGET = "budget"
    CAPABILITY = "capability"
    DIAGNOSTIC = "diagnostic"
    IDEMPOTENCY = "idempotency"
    LOOP = "loop"
    PROFILE = "profile"
    REDACTION = "redaction"
    RETRY = "retry"
    TIMEOUT = "timeout"
    WAIT = "wait"

    def __str__(self) -> str:
        return str(self.value)
