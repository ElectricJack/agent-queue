from enum import Enum


class MessageWaitResponseState(str, Enum):
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTNER_NOT_RUNNING = "partner_not_running"
    PEER_FAILED = "peer_failed"
    PEER_GONE = "peer_gone"
    SATISFIED = "satisfied"
    THREAD_CLOSED = "thread_closed"
    WAITING = "waiting"

    def __str__(self) -> str:
        return str(self.value)
