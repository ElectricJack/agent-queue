from enum import Enum


class ScheduleListArgsStateType0(str, Enum):
    CANCELLED = "cancelled"
    FIRED = "fired"
    PENDING = "pending"

    def __str__(self) -> str:
        return str(self.value)
