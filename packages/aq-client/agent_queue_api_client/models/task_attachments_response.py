from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_attachment_detail import TaskAttachmentDetail


T = TypeVar("T", bound="TaskAttachmentsResponse")


@_attrs_define
class TaskAttachmentsResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        attachments (list[TaskAttachmentDetail] | Unset):
    """

    success: bool | Unset = True
    attachments: list[TaskAttachmentDetail] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        attachments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.attachments, Unset):
            attachments = []
            for attachments_item_data in self.attachments:
                attachments_item = attachments_item_data.to_dict()
                attachments.append(attachments_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if attachments is not UNSET:
            field_dict["attachments"] = attachments

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_attachment_detail import TaskAttachmentDetail

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _attachments = d.pop("attachments", UNSET)
        attachments: list[TaskAttachmentDetail] | Unset = UNSET
        if _attachments is not UNSET:
            attachments = []
            for attachments_item_data in _attachments:
                attachments_item = TaskAttachmentDetail.from_dict(attachments_item_data)

                attachments.append(attachments_item)

        task_attachments_response = cls(
            success=success,
            attachments=attachments,
        )

        task_attachments_response.additional_properties = d
        return task_attachments_response

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
