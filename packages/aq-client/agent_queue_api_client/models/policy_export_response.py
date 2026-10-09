from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.bundle import Bundle
    from ..models.policy_file_preview import PolicyFilePreview


T = TypeVar("T", bound="PolicyExportResponse")


@_attrs_define
class PolicyExportResponse:
    """
    Attributes:
        bundle (Bundle):
        checksum (str):
        files (list[PolicyFilePreview]):
        archive (str):
        success (bool | Unset):  Default: True.
        written (list[str] | Unset):
    """

    bundle: Bundle
    checksum: str
    files: list[PolicyFilePreview]
    archive: str
    success: bool | Unset = True
    written: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        bundle = self.bundle.to_dict()

        checksum = self.checksum

        files = []
        for files_item_data in self.files:
            files_item = files_item_data.to_dict()
            files.append(files_item)

        archive = self.archive

        success = self.success

        written: list[str] | Unset = UNSET
        if not isinstance(self.written, Unset):
            written = self.written

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "bundle": bundle,
                "checksum": checksum,
                "files": files,
                "archive": archive,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if written is not UNSET:
            field_dict["written"] = written

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.bundle import Bundle
        from ..models.policy_file_preview import PolicyFilePreview

        d = dict(src_dict)
        bundle = Bundle.from_dict(d.pop("bundle"))

        checksum = d.pop("checksum")

        files = []
        _files = d.pop("files")
        for files_item_data in _files:
            files_item = PolicyFilePreview.from_dict(files_item_data)

            files.append(files_item)

        archive = d.pop("archive")

        success = d.pop("success", UNSET)

        written = cast(list[str], d.pop("written", UNSET))

        policy_export_response = cls(
            bundle=bundle,
            checksum=checksum,
            files=files,
            archive=archive,
            success=success,
            written=written,
        )

        policy_export_response.additional_properties = d
        return policy_export_response

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
