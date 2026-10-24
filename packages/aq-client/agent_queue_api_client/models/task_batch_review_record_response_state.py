from enum import Enum


class TaskBatchReviewRecordResponseState(str, Enum):
    NEEDS_SCOPE = "needs_scope"
    REVIEWING = "reviewing"

    def __str__(self) -> str:
        return str(self.value)
