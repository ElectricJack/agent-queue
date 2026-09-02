from enum import Enum


class StepExplanationDTORenderer(str, Enum):
    CANONICAL = "canonical"
    CONTRACT = "contract"

    def __str__(self) -> str:
        return str(self.value)
