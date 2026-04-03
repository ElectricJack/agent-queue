from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProjectSummary")


@_attrs_define
class ProjectSummary:
    """
    Attributes:
        id (str):
        name (str):
        status (str | Unset):  Default: ''.
        credit_weight (float | Unset):  Default: 1.0.
        max_concurrent_agents (int | Unset):  Default: 1.
        workspace (None | str | Unset):
        discord_channel_id (None | str | Unset):
    """

    id: str
    name: str
    status: str | Unset = ""
    credit_weight: float | Unset = 1.0
    max_concurrent_agents: int | Unset = 1
    workspace: None | str | Unset = UNSET
    discord_channel_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        status = self.status

        credit_weight = self.credit_weight

        max_concurrent_agents = self.max_concurrent_agents

        workspace: None | str | Unset
        if isinstance(self.workspace, Unset):
            workspace = UNSET
        else:
            workspace = self.workspace

        discord_channel_id: None | str | Unset
        if isinstance(self.discord_channel_id, Unset):
            discord_channel_id = UNSET
        else:
            discord_channel_id = self.discord_channel_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if credit_weight is not UNSET:
            field_dict["credit_weight"] = credit_weight
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if workspace is not UNSET:
            field_dict["workspace"] = workspace
        if discord_channel_id is not UNSET:
            field_dict["discord_channel_id"] = discord_channel_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        status = d.pop("status", UNSET)

        credit_weight = d.pop("credit_weight", UNSET)

        max_concurrent_agents = d.pop("max_concurrent_agents", UNSET)

        def _parse_workspace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace = _parse_workspace(d.pop("workspace", UNSET))

        def _parse_discord_channel_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        discord_channel_id = _parse_discord_channel_id(d.pop("discord_channel_id", UNSET))

        project_summary = cls(
            id=id,
            name=name,
            status=status,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            workspace=workspace,
            discord_channel_id=discord_channel_id,
        )

        project_summary.additional_properties = d
        return project_summary

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
