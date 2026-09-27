from enum import Enum


class CollaborationMemberRecordState(str, Enum):
    ACCEPTED = "accepted"
    INVITED = "invited"
    REMOVED = "removed"

    def __str__(self) -> str:
        return str(self.value)
