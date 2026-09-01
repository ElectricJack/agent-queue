from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.probed_tool_model import ProbedToolModel


T = TypeVar("T", bound="CatalogEntryModel")


@_attrs_define
class CatalogEntryModel:
    """
    Attributes:
        server_name (str):
        scope (str):
        transport (str):
        project_id (None | str | Unset):
        tools (list[ProbedToolModel] | Unset):
        tool_count (int | Unset):  Default: 0.
        last_probed_at (float | Unset):  Default: 0.0.
        last_error (None | str | Unset):
        ok (bool | Unset):  Default: True.
        is_builtin (bool | Unset):  Default: False.
    """

    server_name: str
    scope: str
    transport: str
    project_id: None | str | Unset = UNSET
    tools: list[ProbedToolModel] | Unset = UNSET
    tool_count: int | Unset = 0
    last_probed_at: float | Unset = 0.0
    last_error: None | str | Unset = UNSET
    ok: bool | Unset = True
    is_builtin: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        server_name = self.server_name

        scope = self.scope

        transport = self.transport

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        tools: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tools, Unset):
            tools = []
            for tools_item_data in self.tools:
                tools_item = tools_item_data.to_dict()
                tools.append(tools_item)

        tool_count = self.tool_count

        last_probed_at = self.last_probed_at

        last_error: None | str | Unset
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        else:
            last_error = self.last_error

        ok = self.ok

        is_builtin = self.is_builtin

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "server_name": server_name,
                "scope": scope,
                "transport": transport,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if tools is not UNSET:
            field_dict["tools"] = tools
        if tool_count is not UNSET:
            field_dict["tool_count"] = tool_count
        if last_probed_at is not UNSET:
            field_dict["last_probed_at"] = last_probed_at
        if last_error is not UNSET:
            field_dict["last_error"] = last_error
        if ok is not UNSET:
            field_dict["ok"] = ok
        if is_builtin is not UNSET:
            field_dict["is_builtin"] = is_builtin

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.probed_tool_model import ProbedToolModel  # noqa: PLC0415

        d = dict(src_dict)
        server_name = d.pop("server_name")

        scope = d.pop("scope")

        transport = d.pop("transport")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        _tools = d.pop("tools", UNSET)
        tools: list[ProbedToolModel] | Unset = UNSET
        if _tools is not UNSET:
            tools = []
            for tools_item_data in _tools:
                tools_item = ProbedToolModel.from_dict(tools_item_data)

                tools.append(tools_item)

        tool_count = d.pop("tool_count", UNSET)

        last_probed_at = d.pop("last_probed_at", UNSET)

        def _parse_last_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))

        ok = d.pop("ok", UNSET)

        is_builtin = d.pop("is_builtin", UNSET)

        catalog_entry_model = cls(
            server_name=server_name,
            scope=scope,
            transport=transport,
            project_id=project_id,
            tools=tools,
            tool_count=tool_count,
            last_probed_at=last_probed_at,
            last_error=last_error,
            ok=ok,
            is_builtin=is_builtin,
        )

        catalog_entry_model.additional_properties = d
        return catalog_entry_model

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
