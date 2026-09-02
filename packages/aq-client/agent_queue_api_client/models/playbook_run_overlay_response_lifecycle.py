from enum import Enum


class PlaybookRunOverlayResponseLifecycle(str, Enum):
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    RUNNING = "running"
    TIMED_OUT = "timed_out"

    def __str__(self) -> str:
        return str(self.value)
