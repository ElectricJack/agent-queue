from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.file_inspection_record import FileInspectionRecord


T = TypeVar("T", bound="RecordFileInspectionResponse")


@_attrs_define
class RecordFileInspectionResponse:
    """
    Attributes:
        project_id (str):
        file_path (str):
        key (str):
        record (FileInspectionRecord):
        recorded (bool | Unset):  Default: True.
        warning (None | str | Unset):
    """

    project_id: str
    file_path: str
    key: str
    record: FileInspectionRecord
    recorded: bool | Unset = True
    warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        file_path = self.file_path

        key = self.key

        record = self.record.to_dict()

        recorded = self.recorded

        warning: None | str | Unset
        if isinstance(self.warning, Unset):
            warning = UNSET
        else:
            warning = self.warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "file_path": file_path,
                "key": key,
                "record": record,
            }
        )
        if recorded is not UNSET:
            field_dict["recorded"] = recorded
        if warning is not UNSET:
            field_dict["warning"] = warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.file_inspection_record import FileInspectionRecord  # noqa: PLC0415

        d = dict(src_dict)
        project_id = d.pop("project_id")

        file_path = d.pop("file_path")

        key = d.pop("key")

        record = FileInspectionRecord.from_dict(d.pop("record"))

        recorded = d.pop("recorded", UNSET)

        def _parse_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        warning = _parse_warning(d.pop("warning", UNSET))

        record_file_inspection_response = cls(
            project_id=project_id,
            file_path=file_path,
            key=key,
            record=record,
            recorded=recorded,
            warning=warning,
        )

        record_file_inspection_response.additional_properties = d
        return record_file_inspection_response

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
