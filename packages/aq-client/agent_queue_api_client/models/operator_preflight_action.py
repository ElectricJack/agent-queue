from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OperatorPreflightAction")


@_attrs_define
class OperatorPreflightAction:
    """
    Attributes:
        command (str):
        args_sha256 (str):
        decision_id (None | str | Unset):
    """

    command: str
    args_sha256: str
    decision_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        command = self.command

        args_sha256 = self.args_sha256

        decision_id: None | str | Unset
        if isinstance(self.decision_id, Unset):
            decision_id = UNSET
        else:
            decision_id = self.decision_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "command": command,
                "args_sha256": args_sha256,
            }
        )
        if decision_id is not UNSET:
            field_dict["decision_id"] = decision_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        command = d.pop("command")

        args_sha256 = d.pop("args_sha256")

        def _parse_decision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_id = _parse_decision_id(d.pop("decision_id", UNSET))

        operator_preflight_action = cls(
            command=command,
            args_sha256=args_sha256,
            decision_id=decision_id,
        )

        operator_preflight_action.additional_properties = d
        return operator_preflight_action

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
