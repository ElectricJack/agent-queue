from enum import Enum


class GraphEdgeDTOKind(str, Enum):
    CANCELLED = "cancelled"
    DECISION_CASE = "decision_case"
    DECISION_DEFAULT = "decision_default"
    FAILURE = "failure"
    LOOP_BACK = "loop_back"
    LOOP_BODY = "loop_body"
    LOOP_EXIT = "loop_exit"
    RUNTIME_ERROR = "runtime_error"
    SUCCESS = "success"
    TERMINAL = "terminal"
    TIMEOUT = "timeout"
    WAIT_MATCHED = "wait_matched"

    def __str__(self) -> str:
        return str(self.value)
