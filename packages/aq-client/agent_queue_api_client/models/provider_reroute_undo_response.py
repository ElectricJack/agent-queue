from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.reroute_undo_refusal import RerouteUndoRefusal
    from ..models.reroute_undone import RerouteUndone


T = TypeVar("T", bound="ProviderRerouteUndoResponse")


@_attrs_define
class ProviderRerouteUndoResponse:
    """``provider_reroute_undo`` (D16): which tasks went back, and which were refused.

    Attributes:
        outcome (str):
        success (bool | Unset):  Default: True.
        undone (list[RerouteUndone] | Unset):
        refused (list[RerouteUndoRefusal] | Unset):
    """

    outcome: str
    success: bool | Unset = True
    undone: list[RerouteUndone] | Unset = UNSET
    refused: list[RerouteUndoRefusal] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        outcome = self.outcome

        success = self.success

        undone: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.undone, Unset):
            undone = []
            for undone_item_data in self.undone:
                undone_item = undone_item_data.to_dict()
                undone.append(undone_item)

        refused: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.refused, Unset):
            refused = []
            for refused_item_data in self.refused:
                refused_item = refused_item_data.to_dict()
                refused.append(refused_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "outcome": outcome,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if undone is not UNSET:
            field_dict["undone"] = undone
        if refused is not UNSET:
            field_dict["refused"] = refused

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.reroute_undo_refusal import RerouteUndoRefusal
        from ..models.reroute_undone import RerouteUndone

        d = dict(src_dict)
        outcome = d.pop("outcome")

        success = d.pop("success", UNSET)

        _undone = d.pop("undone", UNSET)
        undone: list[RerouteUndone] | Unset = UNSET
        if _undone is not UNSET:
            undone = []
            for undone_item_data in _undone:
                undone_item = RerouteUndone.from_dict(undone_item_data)

                undone.append(undone_item)

        _refused = d.pop("refused", UNSET)
        refused: list[RerouteUndoRefusal] | Unset = UNSET
        if _refused is not UNSET:
            refused = []
            for refused_item_data in _refused:
                refused_item = RerouteUndoRefusal.from_dict(refused_item_data)

                refused.append(refused_item)

        provider_reroute_undo_response = cls(
            outcome=outcome,
            success=success,
            undone=undone,
            refused=refused,
        )

        provider_reroute_undo_response.additional_properties = d
        return provider_reroute_undo_response

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
