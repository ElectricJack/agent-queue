from enum import Enum


class ListResponseEmptyReasonType0(str, Enum):
    ALL_FINISHED = "all_finished"
    NO_MATCHES = "no_matches"
    NO_WORK = "no_work"

    def __str__(self) -> str:
        return str(self.value)
