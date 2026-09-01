from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.execute_request_args import ExecuteRequestArgs


T = TypeVar("T", bound="ExecuteRequest")


@_attrs_define
class ExecuteRequest:
    """
    Attributes:
        command (str):
        args (ExecuteRequestArgs | Unset):
    """

    command: str
    args: ExecuteRequestArgs | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        command = self.command

        args: dict[str, Any] | Unset = UNSET
        if not isinstance(self.args, Unset):
            args = self.args.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "command": command,
            }
        )
        if args is not UNSET:
            field_dict["args"] = args

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.execute_request_args import ExecuteRequestArgs

        d = dict(src_dict)
        command = d.pop("command")

        _args = d.pop("args", UNSET)
        args: ExecuteRequestArgs | Unset
        if isinstance(_args, Unset):
            args = UNSET
        else:
            args = ExecuteRequestArgs.from_dict(_args)

        execute_request = cls(
            command=command,
            args=args,
        )

        execute_request.additional_properties = d
        return execute_request

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
