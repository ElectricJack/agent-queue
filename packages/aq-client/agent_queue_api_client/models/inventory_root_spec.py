from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="InventoryRootSpec")


@_attrs_define
class InventoryRootSpec:
    """One scan root: an absolute path with a stable, path-free identity.

    Attributes:
        root_id (str):
        path (str):
        source_scope (str):
        source_kind (str | Unset):  Default: 'memory'.
        relative_paths (list[str] | None | Unset):
    """

    root_id: str
    path: str
    source_scope: str
    source_kind: str | Unset = "memory"
    relative_paths: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        root_id = self.root_id

        path = self.path

        source_scope = self.source_scope

        source_kind = self.source_kind

        relative_paths: list[str] | None | Unset
        if isinstance(self.relative_paths, Unset):
            relative_paths = UNSET
        elif isinstance(self.relative_paths, list):
            relative_paths = self.relative_paths

        else:
            relative_paths = self.relative_paths

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "root_id": root_id,
                "path": path,
                "source_scope": source_scope,
            }
        )
        if source_kind is not UNSET:
            field_dict["source_kind"] = source_kind
        if relative_paths is not UNSET:
            field_dict["relative_paths"] = relative_paths

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        root_id = d.pop("root_id")

        path = d.pop("path")

        source_scope = d.pop("source_scope")

        source_kind = d.pop("source_kind", UNSET)

        def _parse_relative_paths(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                relative_paths_type_0 = cast(list[str], data)

                return relative_paths_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        relative_paths = _parse_relative_paths(d.pop("relative_paths", UNSET))

        inventory_root_spec = cls(
            root_id=root_id,
            path=path,
            source_scope=source_scope,
            source_kind=source_kind,
            relative_paths=relative_paths,
        )

        inventory_root_spec.additional_properties = d
        return inventory_root_spec

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
