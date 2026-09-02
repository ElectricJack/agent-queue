from enum import Enum


class NodeOverlayDTOState(str, Enum):
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_VISITED = "not_visited"
    PAUSED = "paused"
    RUNNING = "running"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"

    def __str__(self) -> str:
        return str(self.value)
