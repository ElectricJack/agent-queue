from enum import Enum


class ExplanationValueDTOKind(str, Enum):
    BINDING_REF = "binding_ref"
    EVENT_REF = "event_ref"
    EXPRESSION = "expression"
    LITERAL = "literal"
    LOOP_REF = "loop_ref"
    REDACTED = "redacted"
    TEMPLATE = "template"
    UNRESOLVED = "unresolved"

    def __str__(self) -> str:
        return str(self.value)
