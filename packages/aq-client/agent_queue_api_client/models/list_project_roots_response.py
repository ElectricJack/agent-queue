from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.project_root_info import ProjectRootInfo


T = TypeVar("T", bound="ListProjectRootsResponse")


@_attrs_define
class ListProjectRootsResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        roots (list[ProjectRootInfo] | Unset):
    """

    success: bool | Unset = True
    roots: list[ProjectRootInfo] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        roots: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.roots, Unset):
            roots = []
            for roots_item_data in self.roots:
                roots_item = roots_item_data.to_dict()
                roots.append(roots_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if roots is not UNSET:
            field_dict["roots"] = roots

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.project_root_info import ProjectRootInfo

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _roots = d.pop("roots", UNSET)
        roots: list[ProjectRootInfo] | Unset = UNSET
        if _roots is not UNSET:
            roots = []
            for roots_item_data in _roots:
                roots_item = ProjectRootInfo.from_dict(roots_item_data)

                roots.append(roots_item)

        list_project_roots_response = cls(
            success=success,
            roots=roots,
        )

        list_project_roots_response.additional_properties = d
        return list_project_roots_response

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
