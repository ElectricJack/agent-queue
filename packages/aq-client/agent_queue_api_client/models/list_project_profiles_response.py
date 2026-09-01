from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_project_profiles_response_tool_catalog import ListProjectProfilesResponseToolCatalog
    from ..models.project_profile_row import ProjectProfileRow


T = TypeVar("T", bound="ListProjectProfilesResponse")


@_attrs_define
class ListProjectProfilesResponse:
    """
    Attributes:
        project_id (str):
        agent_types (list[ProjectProfileRow] | Unset):
        tool_catalog (ListProjectProfilesResponseToolCatalog | Unset):
    """

    project_id: str
    agent_types: list[ProjectProfileRow] | Unset = UNSET
    tool_catalog: ListProjectProfilesResponseToolCatalog | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        agent_types: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.agent_types, Unset):
            agent_types = []
            for agent_types_item_data in self.agent_types:
                agent_types_item = agent_types_item_data.to_dict()
                agent_types.append(agent_types_item)

        tool_catalog: dict[str, Any] | Unset = UNSET
        if not isinstance(self.tool_catalog, Unset):
            tool_catalog = self.tool_catalog.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if agent_types is not UNSET:
            field_dict["agent_types"] = agent_types
        if tool_catalog is not UNSET:
            field_dict["tool_catalog"] = tool_catalog

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_project_profiles_response_tool_catalog import (
            ListProjectProfilesResponseToolCatalog,  # noqa: PLC0415
        )
        from ..models.project_profile_row import ProjectProfileRow  # noqa: PLC0415

        d = dict(src_dict)
        project_id = d.pop("project_id")

        _agent_types = d.pop("agent_types", UNSET)
        agent_types: list[ProjectProfileRow] | Unset = UNSET
        if _agent_types is not UNSET:
            agent_types = []
            for agent_types_item_data in _agent_types:
                agent_types_item = ProjectProfileRow.from_dict(agent_types_item_data)

                agent_types.append(agent_types_item)

        _tool_catalog = d.pop("tool_catalog", UNSET)
        tool_catalog: ListProjectProfilesResponseToolCatalog | Unset
        if isinstance(_tool_catalog, Unset):
            tool_catalog = UNSET
        else:
            tool_catalog = ListProjectProfilesResponseToolCatalog.from_dict(_tool_catalog)

        list_project_profiles_response = cls(
            project_id=project_id,
            agent_types=agent_types,
            tool_catalog=tool_catalog,
        )

        list_project_profiles_response.additional_properties = d
        return list_project_profiles_response

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
