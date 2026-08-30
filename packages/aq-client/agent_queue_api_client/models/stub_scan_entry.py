from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="StubScanEntry")


@_attrs_define
class StubScanEntry:
    """
    Attributes:
        stub_name (str):
        status (str):
        source_path (None | str | Unset):
        recorded_hash (None | str | Unset):
        current_hash (None | str | Unset):
        last_synced (None | str | Unset):
        is_enriched (bool | Unset):  Default: False.
    """

    stub_name: str
    status: str
    source_path: None | str | Unset = UNSET
    recorded_hash: None | str | Unset = UNSET
    current_hash: None | str | Unset = UNSET
    last_synced: None | str | Unset = UNSET
    is_enriched: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        stub_name = self.stub_name

        status = self.status

        source_path: None | str | Unset
        if isinstance(self.source_path, Unset):
            source_path = UNSET
        else:
            source_path = self.source_path

        recorded_hash: None | str | Unset
        if isinstance(self.recorded_hash, Unset):
            recorded_hash = UNSET
        else:
            recorded_hash = self.recorded_hash

        current_hash: None | str | Unset
        if isinstance(self.current_hash, Unset):
            current_hash = UNSET
        else:
            current_hash = self.current_hash

        last_synced: None | str | Unset
        if isinstance(self.last_synced, Unset):
            last_synced = UNSET
        else:
            last_synced = self.last_synced

        is_enriched = self.is_enriched

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "stub_name": stub_name,
                "status": status,
            }
        )
        if source_path is not UNSET:
            field_dict["source_path"] = source_path
        if recorded_hash is not UNSET:
            field_dict["recorded_hash"] = recorded_hash
        if current_hash is not UNSET:
            field_dict["current_hash"] = current_hash
        if last_synced is not UNSET:
            field_dict["last_synced"] = last_synced
        if is_enriched is not UNSET:
            field_dict["is_enriched"] = is_enriched

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stub_name = d.pop("stub_name")

        status = d.pop("status")

        def _parse_source_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_path = _parse_source_path(d.pop("source_path", UNSET))

        def _parse_recorded_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recorded_hash = _parse_recorded_hash(d.pop("recorded_hash", UNSET))

        def _parse_current_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_hash = _parse_current_hash(d.pop("current_hash", UNSET))

        def _parse_last_synced(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_synced = _parse_last_synced(d.pop("last_synced", UNSET))

        is_enriched = d.pop("is_enriched", UNSET)

        stub_scan_entry = cls(
            stub_name=stub_name,
            status=status,
            source_path=source_path,
            recorded_hash=recorded_hash,
            current_hash=current_hash,
            last_synced=last_synced,
            is_enriched=is_enriched,
        )

        stub_scan_entry.additional_properties = d
        return stub_scan_entry

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
