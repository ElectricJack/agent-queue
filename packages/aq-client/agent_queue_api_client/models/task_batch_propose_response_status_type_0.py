from enum import Enum


class TaskBatchProposeResponseStatusType0(str, Enum):
    COMMITTED = "committed"
    DISCARDED = "discarded"
    DRAFT = "draft"
    HELD = "held"
    NEEDS_SCOPE = "needs_scope"
    READY = "ready"
    RELEASED = "released"
    RETIRED = "retired"
    REVIEWING = "reviewing"

    def __str__(self) -> str:
        return str(self.value)
