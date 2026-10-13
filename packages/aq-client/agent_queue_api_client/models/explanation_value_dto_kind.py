from enum import Enum


class ExplanationValueDTOKind(str, Enum):
    BINDING_REF = "binding_ref"
    DIFF = "diff"
    EVENT_REF = "event_ref"
    EXPRESSION = "expression"
    FILTER = "filter"
    LEN = "len"
    LITERAL = "literal"
    LOOP_REF = "loop_ref"
    MAP = "map"
    REDACTED = "redacted"
    TEMPLATE = "template"
    UNRESOLVED = "unresolved"

    def __str__(self) -> str:
        return str(self.value)
