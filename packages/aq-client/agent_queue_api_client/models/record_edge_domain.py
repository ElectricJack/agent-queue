from enum import Enum


class RecordEdgeDomain(str, Enum):
    EXECUTION = "execution"
    INFORMATIONAL = "informational"

    def __str__(self) -> str:
        return str(self.value)
