from enum import Enum


class PendingPullRequestReviewDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes requested"
    PENDING = "pending"

    def __str__(self) -> str:
        return str(self.value)
