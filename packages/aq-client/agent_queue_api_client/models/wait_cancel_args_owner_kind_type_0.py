from enum import Enum


class WaitCancelArgsOwnerKindType0(str, Enum):
    INTEGRATION = "integration"
    PLAYBOOK = "playbook"
    SCHEDULER = "scheduler"

    def __str__(self) -> str:
        return str(self.value)
