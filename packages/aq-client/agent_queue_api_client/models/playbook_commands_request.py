from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookCommandsRequest")


@_attrs_define
class PlaybookCommandsRequest:
    """
    Attributes:
        name (None | str | Unset):
        prefix (None | str | Unset):
        origin (None | str | Unset):
        plugin (None | str | Unset):
        schema_quality (None | str | Unset):
        result_mode (None | str | Unset):
        available (bool | None | Unset):
        denied (bool | None | Unset):
        include_schema (bool | Unset):  Default: True.
        offset (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 50.
    """

    name: None | str | Unset = UNSET
    prefix: None | str | Unset = UNSET
    origin: None | str | Unset = UNSET
    plugin: None | str | Unset = UNSET
    schema_quality: None | str | Unset = UNSET
    result_mode: None | str | Unset = UNSET
    available: bool | None | Unset = UNSET
    denied: bool | None | Unset = UNSET
    include_schema: bool | Unset = True
    offset: int | Unset = 0
    limit: int | Unset = 50
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        prefix: None | str | Unset
        if isinstance(self.prefix, Unset):
            prefix = UNSET
        else:
            prefix = self.prefix

        origin: None | str | Unset
        if isinstance(self.origin, Unset):
            origin = UNSET
        else:
            origin = self.origin

        plugin: None | str | Unset
        if isinstance(self.plugin, Unset):
            plugin = UNSET
        else:
            plugin = self.plugin

        schema_quality: None | str | Unset
        if isinstance(self.schema_quality, Unset):
            schema_quality = UNSET
        else:
            schema_quality = self.schema_quality

        result_mode: None | str | Unset
        if isinstance(self.result_mode, Unset):
            result_mode = UNSET
        else:
            result_mode = self.result_mode

        available: bool | None | Unset
        if isinstance(self.available, Unset):
            available = UNSET
        else:
            available = self.available

        denied: bool | None | Unset
        if isinstance(self.denied, Unset):
            denied = UNSET
        else:
            denied = self.denied

        include_schema = self.include_schema

        offset = self.offset

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if prefix is not UNSET:
            field_dict["prefix"] = prefix
        if origin is not UNSET:
            field_dict["origin"] = origin
        if plugin is not UNSET:
            field_dict["plugin"] = plugin
        if schema_quality is not UNSET:
            field_dict["schema_quality"] = schema_quality
        if result_mode is not UNSET:
            field_dict["result_mode"] = result_mode
        if available is not UNSET:
            field_dict["available"] = available
        if denied is not UNSET:
            field_dict["denied"] = denied
        if include_schema is not UNSET:
            field_dict["include_schema"] = include_schema
        if offset is not UNSET:
            field_dict["offset"] = offset
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_prefix(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prefix = _parse_prefix(d.pop("prefix", UNSET))

        def _parse_origin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        origin = _parse_origin(d.pop("origin", UNSET))

        def _parse_plugin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plugin = _parse_plugin(d.pop("plugin", UNSET))

        def _parse_schema_quality(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        schema_quality = _parse_schema_quality(d.pop("schema_quality", UNSET))

        def _parse_result_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_mode = _parse_result_mode(d.pop("result_mode", UNSET))

        def _parse_available(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        available = _parse_available(d.pop("available", UNSET))

        def _parse_denied(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        denied = _parse_denied(d.pop("denied", UNSET))

        include_schema = d.pop("include_schema", UNSET)

        offset = d.pop("offset", UNSET)

        limit = d.pop("limit", UNSET)

        playbook_commands_request = cls(
            name=name,
            prefix=prefix,
            origin=origin,
            plugin=plugin,
            schema_quality=schema_quality,
            result_mode=result_mode,
            available=available,
            denied=denied,
            include_schema=include_schema,
            offset=offset,
            limit=limit,
        )

        playbook_commands_request.additional_properties = d
        return playbook_commands_request

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
