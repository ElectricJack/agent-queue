from enum import Enum


class V1RunSummaryDTOOwnership(str, Enum):
    LIVE = "live"
    ORPHANED = "orphaned"

    def __str__(self) -> str:
        return str(self.value)
