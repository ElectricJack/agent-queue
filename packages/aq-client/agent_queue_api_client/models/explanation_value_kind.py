from enum import Enum


class ExplanationValueKind(str, Enum):
    BINDING_REF = "binding_ref"
    EVENT_REF = "event_ref"
    LITERAL = "literal"
    LOOP_REF = "loop_ref"
    TEMPLATE = "template"
    UNRESOLVED = "unresolved"

    def __str__(self) -> str:
        return str(self.value)
