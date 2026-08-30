from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListMcpToolCatalogRequest")


@_attrs_define
class ListMcpToolCatalogRequest:
    """
    Attributes:
        project_id (None | str | Unset):
        server_names (list[Any] | None | Unset):
    """

    project_id: None | str | Unset = UNSET
    server_names: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        server_names: list[Any] | None | Unset
        if isinstance(self.server_names, Unset):
            server_names = UNSET
        elif isinstance(self.server_names, list):
            server_names = self.server_names

        else:
            server_names = self.server_names

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if server_names is not UNSET:
            field_dict["server_names"] = server_names

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_server_names(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                server_names_type_0 = cast(list[Any], data)

                return server_names_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        server_names = _parse_server_names(d.pop("server_names", UNSET))

        list_mcp_tool_catalog_request = cls(
            project_id=project_id,
            server_names=server_names,
        )

        list_mcp_tool_catalog_request.additional_properties = d
        return list_mcp_tool_catalog_request

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
