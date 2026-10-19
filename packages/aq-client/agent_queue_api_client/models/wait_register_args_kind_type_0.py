from enum import Enum


class WaitRegisterArgsKindType0(str, Enum):
    BATCH = "batch"
    CI_CHECK = "ci_check"
    CI_RUN = "ci_run"
    DEPENDENCY = "dependency"
    HUMAN = "human"
    JOB = "job"
    MESSAGE = "message"
    PLAYBOOK_CONDITION = "playbook_condition"
    PULL_REQUEST = "pull_request"
    RESOURCE = "resource"
    TASK = "task"
    TIMER = "timer"

    def __str__(self) -> str:
        return str(self.value)
