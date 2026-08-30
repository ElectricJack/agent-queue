from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ResumePlaybookResponse")


@_attrs_define
class ResumePlaybookResponse:
    """
    Attributes:
        resumed (str):
        playbook_id (str):
        status (str):
        tokens_used (int | Unset):  Default: 0.
    """

    resumed: str
    playbook_id: str
    status: str
    tokens_used: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        resumed = self.resumed

        playbook_id = self.playbook_id

        status = self.status

        tokens_used = self.tokens_used

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "resumed": resumed,
                "playbook_id": playbook_id,
                "status": status,
            }
        )
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        resumed = d.pop("resumed")

        playbook_id = d.pop("playbook_id")

        status = d.pop("status")

        tokens_used = d.pop("tokens_used", UNSET)

        resume_playbook_response = cls(
            resumed=resumed,
            playbook_id=playbook_id,
            status=status,
            tokens_used=tokens_used,
        )

        resume_playbook_response.additional_properties = d
        return resume_playbook_response

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
