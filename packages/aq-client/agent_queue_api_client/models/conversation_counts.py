from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.conversation_counts_by_state import ConversationCountsByState


T = TypeVar("T", bound="ConversationCounts")


@_attrs_define
class ConversationCounts:
    """
    Attributes:
        by_state (ConversationCountsByState):
        inputs_pending_supervisor (int):
    """

    by_state: ConversationCountsByState
    inputs_pending_supervisor: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        by_state = self.by_state.to_dict()

        inputs_pending_supervisor = self.inputs_pending_supervisor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "by_state": by_state,
                "inputs_pending_supervisor": inputs_pending_supervisor,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_counts_by_state import ConversationCountsByState

        d = dict(src_dict)
        by_state = ConversationCountsByState.from_dict(d.pop("by_state"))

        inputs_pending_supervisor = d.pop("inputs_pending_supervisor")

        conversation_counts = cls(
            by_state=by_state,
            inputs_pending_supervisor=inputs_pending_supervisor,
        )

        conversation_counts.additional_properties = d
        return conversation_counts

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
