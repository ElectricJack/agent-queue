from enum import Enum


class DiffItemStatus(str, Enum):
    IDENTICAL = "identical"
    NEW = "new"
    WILL_OVERWRITE = "will overwrite"

    def __str__(self) -> str:
        return str(self.value)
