from enum import Enum


class TemplatePolicyKind(str, Enum):
    FORMULA = "formula"
    PLAN = "plan"
    SPEC = "spec"

    def __str__(self) -> str:
        return str(self.value)
