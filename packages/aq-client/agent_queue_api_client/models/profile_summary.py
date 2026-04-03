from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileSummary")


@_attrs_define
class ProfileSummary:
    """
    Attributes:
        id (str):
        name (str):
        description (str | Unset):  Default: ''.
        model (str | Unset):  Default: ''.
        allowed_tools (list[str] | Unset):
        mcp_servers (list[str] | Unset):
        has_system_prompt (bool | Unset):  Default: False.
    """

    id: str
    name: str
    description: str | Unset = ""
    model: str | Unset = ""
    allowed_tools: list[str] | Unset = UNSET
    mcp_servers: list[str] | Unset = UNSET
    has_system_prompt: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description = self.description

        model = self.model

        allowed_tools: list[str] | Unset = UNSET
        if not isinstance(self.allowed_tools, Unset):
            allowed_tools = self.allowed_tools

        mcp_servers: list[str] | Unset = UNSET
        if not isinstance(self.mcp_servers, Unset):
            mcp_servers = self.mcp_servers

        has_system_prompt = self.has_system_prompt

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if model is not UNSET:
            field_dict["model"] = model
        if allowed_tools is not UNSET:
            field_dict["allowed_tools"] = allowed_tools
        if mcp_servers is not UNSET:
            field_dict["mcp_servers"] = mcp_servers
        if has_system_prompt is not UNSET:
            field_dict["has_system_prompt"] = has_system_prompt

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description", UNSET)

        model = d.pop("model", UNSET)

        allowed_tools = cast(list[str], d.pop("allowed_tools", UNSET))

        mcp_servers = cast(list[str], d.pop("mcp_servers", UNSET))

        has_system_prompt = d.pop("has_system_prompt", UNSET)

        profile_summary = cls(
            id=id,
            name=name,
            description=description,
            model=model,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            has_system_prompt=has_system_prompt,
        )

        profile_summary.additional_properties = d
        return profile_summary

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
