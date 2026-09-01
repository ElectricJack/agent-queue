from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_summary import PlaybookSummary


T = TypeVar("T", bound="ListPlaybooksResponse")


@_attrs_define
class ListPlaybooksResponse:
    """
    Attributes:
        playbooks (list[PlaybookSummary] | Unset):
        count (int | Unset):  Default: 0.
    """

    playbooks: list[PlaybookSummary] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbooks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.playbooks, Unset):
            playbooks = []
            for playbooks_item_data in self.playbooks:
                playbooks_item = playbooks_item_data.to_dict()
                playbooks.append(playbooks_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if playbooks is not UNSET:
            field_dict["playbooks"] = playbooks
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_summary import PlaybookSummary  # noqa: PLC0415

        d = dict(src_dict)
        _playbooks = d.pop("playbooks", UNSET)
        playbooks: list[PlaybookSummary] | Unset = UNSET
        if _playbooks is not UNSET:
            playbooks = []
            for playbooks_item_data in _playbooks:
                playbooks_item = PlaybookSummary.from_dict(playbooks_item_data)

                playbooks.append(playbooks_item)

        count = d.pop("count", UNSET)

        list_playbooks_response = cls(
            playbooks=playbooks,
            count=count,
        )

        list_playbooks_response.additional_properties = d
        return list_playbooks_response

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
