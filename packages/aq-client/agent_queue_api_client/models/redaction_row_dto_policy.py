from enum import Enum


class RedactionRowDTOPolicy(str, Enum):
    OPAQUE_HANDLE = "opaque_handle"
    REDACTED = "redacted"
    SAFE = "safe"
    SUMMARIZED = "summarized"

    def __str__(self) -> str:
        return str(self.value)
