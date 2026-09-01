from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.log_entry import LogEntry


T = TypeVar("T", bound="ReadLogsResponse")


@_attrs_define
class ReadLogsResponse:
    """
    Attributes:
        log_file (str):
        level_filter (str):
        count (int | Unset):  Default: 0.
        entries (list[LogEntry] | Unset):
    """

    log_file: str
    level_filter: str
    count: int | Unset = 0
    entries: list[LogEntry] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        log_file = self.log_file

        level_filter = self.level_filter

        count = self.count

        entries: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.entries, Unset):
            entries = []
            for entries_item_data in self.entries:
                entries_item = entries_item_data.to_dict()
                entries.append(entries_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "log_file": log_file,
                "level_filter": level_filter,
            }
        )
        if count is not UNSET:
            field_dict["count"] = count
        if entries is not UNSET:
            field_dict["entries"] = entries

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.log_entry import LogEntry  # noqa: PLC0415

        d = dict(src_dict)
        log_file = d.pop("log_file")

        level_filter = d.pop("level_filter")

        count = d.pop("count", UNSET)

        _entries = d.pop("entries", UNSET)
        entries: list[LogEntry] | Unset = UNSET
        if _entries is not UNSET:
            entries = []
            for entries_item_data in _entries:
                entries_item = LogEntry.from_dict(entries_item_data)

                entries.append(entries_item)

        read_logs_response = cls(
            log_file=log_file,
            level_filter=level_filter,
            count=count,
            entries=entries,
        )

        read_logs_response.additional_properties = d
        return read_logs_response

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
