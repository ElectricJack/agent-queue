from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_attachment_detail import TaskAttachmentDetail


T = TypeVar("T", bound="TaskAttachmentResponse")


@_attrs_define
class TaskAttachmentResponse:
    """
    Attributes:
        attachment (TaskAttachmentDetail):
        success (bool | Unset):  Default: True.
    """

    attachment: TaskAttachmentDetail
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        attachment = self.attachment.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "attachment": attachment,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_attachment_detail import TaskAttachmentDetail

        d = dict(src_dict)
        attachment = TaskAttachmentDetail.from_dict(d.pop("attachment"))

        success = d.pop("success", UNSET)

        task_attachment_response = cls(
            attachment=attachment,
            success=success,
        )

        task_attachment_response.additional_properties = d
        return task_attachment_response

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
