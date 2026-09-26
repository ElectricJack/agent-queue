from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.conversation_backfill_cursors_item import ConversationBackfillCursorsItem
    from ..models.conversation_backfill_gaps_item import ConversationBackfillGapsItem


T = TypeVar("T", bound="ConversationBackfill")


@_attrs_define
class ConversationBackfill:
    """
    Attributes:
        cursors (list[ConversationBackfillCursorsItem]):
        gaps (list[ConversationBackfillGapsItem]):
    """

    cursors: list[ConversationBackfillCursorsItem]
    gaps: list[ConversationBackfillGapsItem]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cursors = []
        for cursors_item_data in self.cursors:
            cursors_item = cursors_item_data.to_dict()
            cursors.append(cursors_item)

        gaps = []
        for gaps_item_data in self.gaps:
            gaps_item = gaps_item_data.to_dict()
            gaps.append(gaps_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "cursors": cursors,
                "gaps": gaps,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_backfill_cursors_item import ConversationBackfillCursorsItem
        from ..models.conversation_backfill_gaps_item import ConversationBackfillGapsItem

        d = dict(src_dict)
        cursors = []
        _cursors = d.pop("cursors")
        for cursors_item_data in _cursors:
            cursors_item = ConversationBackfillCursorsItem.from_dict(cursors_item_data)

            cursors.append(cursors_item)

        gaps = []
        _gaps = d.pop("gaps")
        for gaps_item_data in _gaps:
            gaps_item = ConversationBackfillGapsItem.from_dict(gaps_item_data)

            gaps.append(gaps_item)

        conversation_backfill = cls(
            cursors=cursors,
            gaps=gaps,
        )

        conversation_backfill.additional_properties = d
        return conversation_backfill

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
