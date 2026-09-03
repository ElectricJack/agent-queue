from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="MigrationReasonDTO")


@_attrs_define
class MigrationReasonDTO:
    """One operator-facing explanation for an entry's disposition.

    ``code`` is drawn from ``src.playbooks.migration.REASON_CODES``, a closed
    set the CLI and the cutover report both switch on.

        Attributes:
            code (str):
            message (str):
            source_line (int | None | Unset):
    """

    code: str
    message: str
    source_line: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        message = self.message

        source_line: int | None | Unset
        if isinstance(self.source_line, Unset):
            source_line = UNSET
        else:
            source_line = self.source_line

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "code": code,
                "message": message,
            }
        )
        if source_line is not UNSET:
            field_dict["source_line"] = source_line

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        message = d.pop("message")

        def _parse_source_line(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        source_line = _parse_source_line(d.pop("source_line", UNSET))

        migration_reason_dto = cls(
            code=code,
            message=message,
            source_line=source_line,
        )

        return migration_reason_dto
