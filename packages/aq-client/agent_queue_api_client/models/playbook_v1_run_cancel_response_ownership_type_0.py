from enum import Enum


class PlaybookV1RunCancelResponseOwnershipType0(str, Enum):
    LIVE = "live"
    ORPHANED = "orphaned"

    def __str__(self) -> str:
        return str(self.value)
