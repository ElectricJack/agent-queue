from enum import Enum


class OperatorDecisionDTOOptionsItem(str, Enum):
    ACCEPT_OUTCOME = "accept_outcome"
    CANCEL = "cancel"
    FAIL = "fail"
    RETRY = "retry"

    def __str__(self) -> str:
        return str(self.value)
