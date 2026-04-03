from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RestartTaskResponse")


@_attrs_define
class RestartTaskResponse:
    """
    Attributes:
        restarted (str):
        title (str):
        previous_status (str | Unset):  Default: ''.
    """

    restarted: str
    title: str
    previous_status: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        restarted = self.restarted

        title = self.title

        previous_status = self.previous_status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "restarted": restarted,
                "title": title,
            }
        )
        if previous_status is not UNSET:
            field_dict["previous_status"] = previous_status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        restarted = d.pop("restarted")

        title = d.pop("title")

        previous_status = d.pop("previous_status", UNSET)

        restart_task_response = cls(
            restarted=restarted,
            title=title,
            previous_status=previous_status,
        )

        restart_task_response.additional_properties = d
        return restart_task_response

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
