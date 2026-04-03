from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_available_tools_response_mcp_servers_item import ListAvailableToolsResponseMcpServersItem
    from ..models.list_available_tools_response_tools_item import ListAvailableToolsResponseToolsItem


T = TypeVar("T", bound="ListAvailableToolsResponse")


@_attrs_define
class ListAvailableToolsResponse:
    """
    Attributes:
        tools (list[ListAvailableToolsResponseToolsItem] | Unset):
        mcp_servers (list[ListAvailableToolsResponseMcpServersItem] | Unset):
    """

    tools: list[ListAvailableToolsResponseToolsItem] | Unset = UNSET
    mcp_servers: list[ListAvailableToolsResponseMcpServersItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        tools: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tools, Unset):
            tools = []
            for tools_item_data in self.tools:
                tools_item = tools_item_data.to_dict()
                tools.append(tools_item)

        mcp_servers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.mcp_servers, Unset):
            mcp_servers = []
            for mcp_servers_item_data in self.mcp_servers:
                mcp_servers_item = mcp_servers_item_data.to_dict()
                mcp_servers.append(mcp_servers_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if tools is not UNSET:
            field_dict["tools"] = tools
        if mcp_servers is not UNSET:
            field_dict["mcp_servers"] = mcp_servers

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_available_tools_response_mcp_servers_item import ListAvailableToolsResponseMcpServersItem
        from ..models.list_available_tools_response_tools_item import ListAvailableToolsResponseToolsItem

        d = dict(src_dict)
        _tools = d.pop("tools", UNSET)
        tools: list[ListAvailableToolsResponseToolsItem] | Unset = UNSET
        if _tools is not UNSET:
            tools = []
            for tools_item_data in _tools:
                tools_item = ListAvailableToolsResponseToolsItem.from_dict(tools_item_data)

                tools.append(tools_item)

        _mcp_servers = d.pop("mcp_servers", UNSET)
        mcp_servers: list[ListAvailableToolsResponseMcpServersItem] | Unset = UNSET
        if _mcp_servers is not UNSET:
            mcp_servers = []
            for mcp_servers_item_data in _mcp_servers:
                mcp_servers_item = ListAvailableToolsResponseMcpServersItem.from_dict(mcp_servers_item_data)

                mcp_servers.append(mcp_servers_item)

        list_available_tools_response = cls(
            tools=tools,
            mcp_servers=mcp_servers,
        )

        list_available_tools_response.additional_properties = d
        return list_available_tools_response

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
