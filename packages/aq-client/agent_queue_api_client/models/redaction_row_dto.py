from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.redaction_row_dto_policy import RedactionRowDTOPolicy

T = TypeVar("T", bound="RedactionRowDTO")


@_attrs_define
class RedactionRowDTO:
    """
    Attributes:
        field (str):
        policy (RedactionRowDTOPolicy):
    """

    field: str
    policy: RedactionRowDTOPolicy

    def to_dict(self) -> dict[str, Any]:
        field = self.field

        policy = self.policy.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "field": field,
                "policy": policy,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        field = d.pop("field")

        policy = RedactionRowDTOPolicy(d.pop("policy"))

        redaction_row_dto = cls(
            field=field,
            policy=policy,
        )

        return redaction_row_dto
