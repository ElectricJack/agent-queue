from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.stub_scan_entry import StubScanEntry


T = TypeVar("T", bound="StubScanProject")


@_attrs_define
class StubScanProject:
    """
    Attributes:
        project_id (str):
        total (int | Unset):  Default: 0.
        stale (int | Unset):  Default: 0.
        missing_source (int | Unset):  Default: 0.
        unenriched (int | Unset):  Default: 0.
        orphaned (int | Unset):  Default: 0.
        current (int | Unset):  Default: 0.
        stubs (list[StubScanEntry] | Unset):
    """

    project_id: str
    total: int | Unset = 0
    stale: int | Unset = 0
    missing_source: int | Unset = 0
    unenriched: int | Unset = 0
    orphaned: int | Unset = 0
    current: int | Unset = 0
    stubs: list[StubScanEntry] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        total = self.total

        stale = self.stale

        missing_source = self.missing_source

        unenriched = self.unenriched

        orphaned = self.orphaned

        current = self.current

        stubs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stubs, Unset):
            stubs = []
            for stubs_item_data in self.stubs:
                stubs_item = stubs_item_data.to_dict()
                stubs.append(stubs_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
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
        if stubs is not UNSET:
            field_dict["stubs"] = stubs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stub_scan_entry import StubScanEntry

        d = dict(src_dict)
        project_id = d.pop("project_id")

        total = d.pop("total", UNSET)

        stale = d.pop("stale", UNSET)

        missing_source = d.pop("missing_source", UNSET)

        unenriched = d.pop("unenriched", UNSET)

        orphaned = d.pop("orphaned", UNSET)

        current = d.pop("current", UNSET)

        _stubs = d.pop("stubs", UNSET)
        stubs: list[StubScanEntry] | Unset = UNSET
        if _stubs is not UNSET:
            stubs = []
            for stubs_item_data in _stubs:
                stubs_item = StubScanEntry.from_dict(stubs_item_data)

                stubs.append(stubs_item)

        stub_scan_project = cls(
            project_id=project_id,
            total=total,
            stale=stale,
            missing_source=missing_source,
            unenriched=unenriched,
            orphaned=orphaned,
            current=current,
            stubs=stubs,
        )

        stub_scan_project.additional_properties = d
        return stub_scan_project

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
