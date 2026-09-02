from enum import Enum


class CompilerDiagnosticDTOSeverity(str, Enum):
    ERROR = "error"
    INFO = "info"
    QUESTION = "question"
    WARNING = "warning"

    def __str__(self) -> str:
        return str(self.value)
