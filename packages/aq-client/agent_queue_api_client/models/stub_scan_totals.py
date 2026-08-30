from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="StubScanTotals")


@_attrs_define
class StubScanTotals:
    """
    Attributes:
        total (int | Unset):  Default: 0.
        stale (int | Unset):  Default: 0.
        missing_source (int | Unset):  Default: 0.
        unenriched (int | Unset):  Default: 0.
        orphaned (int | Unset):  Default: 0.
        current (int | Unset):  Default: 0.
    """

    total: int | Unset = 0
    stale: int | Unset = 0
    missing_source: int | Unset = 0
    unenriched: int | Unset = 0
    orphaned: int | Unset = 0
    current: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        stale = self.stale

        missing_source = self.missing_source

        unenriched = self.unenriched

        orphaned = self.orphaned

        current = self.current

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if total is not UNSET:
            field_dict["total"] = total
        if stale is not UNSET:
            field_dict["stale"] = stale
        if missing_source is not UNSET:
            field_dict["missing_source"] = missing_source
        if unenriched is not UNSET:
            field_dict["unenriched"] = unenriched
        if orphaned is not UNSET:
            field_dict["orphaned"] = orphaned
        if current is not UNSET:
            field_dict["current"] = current

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        total = d.pop("total", UNSET)

        stale = d.pop("stale", UNSET)

        missing_source = d.pop("missing_source", UNSET)

        unenriched = d.pop("unenriched", UNSET)

        orphaned = d.pop("orphaned", UNSET)

        current = d.pop("current", UNSET)

        stub_scan_totals = cls(
            total=total,
            stale=stale,
            missing_source=missing_source,
            unenriched=unenriched,
            orphaned=orphaned,
            current=current,
        )

        stub_scan_totals.additional_properties = d
        return stub_scan_totals

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
