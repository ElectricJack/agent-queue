from enum import Enum


class ExplanationRowDTOSource(str, Enum):
    BINDING = "binding"
    DERIVED = "derived"
    EVENT = "event"
    LITERAL = "literal"
    LOOP = "loop"
    POLICY = "policy"
    PROFILE = "profile"
    TEMPLATE = "template"

    def __str__(self) -> str:
        return str(self.value)
