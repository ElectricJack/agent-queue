from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RecordRepairRequest")


@_attrs_define
class RecordRepairRequest:
    """
    Attributes:
        operation (str):
        dry_run (bool | Unset):  Default: True.
        event_id (None | str | Unset):
        max_batches (int | Unset):  Default: 2.
    """

    operation: str
    dry_run: bool | Unset = True
    event_id: None | str | Unset = UNSET
    max_batches: int | Unset = 2
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        operation = self.operation

        dry_run = self.dry_run

        event_id: None | str | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        max_batches = self.max_batches

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "operation": operation,
            }
        )
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if event_id is not UNSET:
            field_dict["event_id"] = event_id
        if max_batches is not UNSET:
            field_dict["max_batches"] = max_batches

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        operation = d.pop("operation")

        dry_run = d.pop("dry_run", UNSET)

        def _parse_event_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        max_batches = d.pop("max_batches", UNSET)

        record_repair_request = cls(
            operation=operation,
            dry_run=dry_run,
            event_id=event_id,
            max_batches=max_batches,
        )

        record_repair_request.additional_properties = d
        return record_repair_request

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
