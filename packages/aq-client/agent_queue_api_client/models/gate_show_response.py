from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gate_summary import GateSummary


T = TypeVar("T", bound="GateShowResponse")


@_attrs_define
class GateShowResponse:
    """
    Attributes:
        gate (GateSummary): Gate row as returned by ``gate_list`` / ``gate_show``.

            Fields come from ``src/database/queries/gate_queries.py`` which returns
            dict rows including the resolved metadata columns.
        success (bool | Unset):  Default: True.
        waiters (list[str] | Unset):
    """

    gate: GateSummary
    success: bool | Unset = True
    waiters: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate = self.gate.to_dict()

        success = self.success

        waiters: list[str] | Unset = UNSET
        if not isinstance(self.waiters, Unset):
            waiters = self.waiters

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate": gate,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if waiters is not UNSET:
            field_dict["waiters"] = waiters

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_summary import GateSummary

        d = dict(src_dict)
        gate = GateSummary.from_dict(d.pop("gate"))

        success = d.pop("success", UNSET)

        waiters = cast(list[str], d.pop("waiters", UNSET))

        gate_show_response = cls(
            gate=gate,
            success=success,
            waiters=waiters,
        )

        gate_show_response.additional_properties = d
        return gate_show_response

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
