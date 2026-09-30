from enum import Enum


class ObjectScoreRecordResponseOutcome(str, Enum):
    CHECKPOINT = "checkpoint"
    CONTINUE = "continue"
    REUSED = "reused"
    STOP = "stop"

    def __str__(self) -> str:
        return str(self.value)
