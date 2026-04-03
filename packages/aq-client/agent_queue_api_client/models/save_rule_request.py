from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SaveRuleRequest")


@_attrs_define
class SaveRuleRequest:
    """
    Attributes:
        type_ (str): Rule type: 'active' for triggered automation, 'passive' for reasoning guidance
        content (str): Rule content (markdown with # Title, ## Trigger, ## Logic sections)
        id (None | str | Unset): Rule ID (auto-generated if omitted)
        project_id (None | str | Unset): Project ID (null = global rule visible to all projects)
    """

    type_: str
    content: str
    id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        content = self.content

        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "content": content,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        type_ = d.pop("type")

        content = d.pop("content")

        def _parse_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        save_rule_request = cls(
            type_=type_,
            content=content,
            id=id,
            project_id=project_id,
        )

        save_rule_request.additional_properties = d
        return save_rule_request

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
