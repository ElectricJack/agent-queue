"""Transport-independent record errors and qualified identities."""

from dataclasses import dataclass
from uuid import UUID


class RecordError(ValueError):
    def __init__(self, code: str, message: str | None = None, **details):
        super().__init__(message or code)
        self.code = code
        self.details = details

    def result(self) -> dict:
        return {"success": False, "error_code": self.code, "error": str(self), **self.details}


@dataclass(frozen=True)
class RecordIdentity:
    kind: str
    value: str | UUID

    @classmethod
    def parse(cls, value: str) -> "RecordIdentity":
        if not isinstance(value, str):
            raise RecordError("record.invalid_identity")
        kind, separator, ident = value.partition(":")
        if not separator or not ident or kind not in {"task", "knowledge", "record"}:
            raise RecordError(
                "record.invalid_identity", "Use task:, knowledge: or record: identity"
            )
        if kind == "record":
            try:
                return cls(kind, UUID(ident))
            except ValueError:
                raise RecordError("record.invalid_identity") from None
        if kind == "knowledge":
            ident = ident.lower()
            if len(ident) != 35 or not ident.startswith("kn-"):
                raise RecordError("record.invalid_identity")
            try:
                UUID(hex=ident[3:])
            except ValueError:
                raise RecordError("record.invalid_identity") from None
        return cls(kind, ident)


def uuid_value(value, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise RecordError("record.invalid_input", f"{field} must be a UUID") from None
