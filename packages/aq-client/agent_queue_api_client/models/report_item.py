from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReportItem")


@_attrs_define
class ReportItem:
    """
    Attributes:
        refs (list[str]):
        text (str):
        task_id (None | str | Unset):
        late (bool | Unset):  Default: False.
        shipment (None | str | Unset):
        prior_verification (str | Unset):  Default: ''.
        verification_label (str | Unset):  Default: 'agent-reported'.
    """

    refs: list[str]
    text: str
    task_id: None | str | Unset = UNSET
    late: bool | Unset = False
    shipment: None | str | Unset = UNSET
    prior_verification: str | Unset = ""
    verification_label: str | Unset = "agent-reported"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        refs = self.refs

        text = self.text

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        late = self.late

        shipment: None | str | Unset
        if isinstance(self.shipment, Unset):
            shipment = UNSET
        else:
            shipment = self.shipment

        prior_verification = self.prior_verification

        verification_label = self.verification_label

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "refs": refs,
                "text": text,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if late is not UNSET:
            field_dict["late"] = late
        if shipment is not UNSET:
            field_dict["shipment"] = shipment
        if prior_verification is not UNSET:
            field_dict["prior_verification"] = prior_verification
        if verification_label is not UNSET:
            field_dict["verification_label"] = verification_label

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        refs = cast(list[str], d.pop("refs"))

        text = d.pop("text")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        late = d.pop("late", UNSET)

        def _parse_shipment(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        shipment = _parse_shipment(d.pop("shipment", UNSET))

        prior_verification = d.pop("prior_verification", UNSET)

        verification_label = d.pop("verification_label", UNSET)

        report_item = cls(
            refs=refs,
            text=text,
            task_id=task_id,
            late=late,
            shipment=shipment,
            prior_verification=prior_verification,
            verification_label=verification_label,
        )

        report_item.additional_properties = d
        return report_item

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
