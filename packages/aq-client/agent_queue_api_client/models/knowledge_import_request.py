from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_import_request_scope_aliases_type_0 import KnowledgeImportRequestScopeAliasesType0


T = TypeVar("T", bound="KnowledgeImportRequest")


@_attrs_define
class KnowledgeImportRequest:
    """
    Attributes:
        roots (list[Any]):
        source_installation_id (str):
        snapshot_id (str):
        snapshot_timestamp (str):
        vector_export (None | str | Unset):
        scope_aliases (KnowledgeImportRequestScopeAliasesType0 | None | Unset):
    """

    roots: list[Any]
    source_installation_id: str
    snapshot_id: str
    snapshot_timestamp: str
    vector_export: None | str | Unset = UNSET
    scope_aliases: KnowledgeImportRequestScopeAliasesType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_import_request_scope_aliases_type_0 import KnowledgeImportRequestScopeAliasesType0

        roots = self.roots

        source_installation_id = self.source_installation_id

        snapshot_id = self.snapshot_id

        snapshot_timestamp = self.snapshot_timestamp

        vector_export: None | str | Unset
        if isinstance(self.vector_export, Unset):
            vector_export = UNSET
        else:
            vector_export = self.vector_export

        scope_aliases: dict[str, Any] | None | Unset
        if isinstance(self.scope_aliases, Unset):
            scope_aliases = UNSET
        elif isinstance(self.scope_aliases, KnowledgeImportRequestScopeAliasesType0):
            scope_aliases = self.scope_aliases.to_dict()
        else:
            scope_aliases = self.scope_aliases

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "roots": roots,
                "source_installation_id": source_installation_id,
                "snapshot_id": snapshot_id,
                "snapshot_timestamp": snapshot_timestamp,
            }
        )
        if vector_export is not UNSET:
            field_dict["vector_export"] = vector_export
        if scope_aliases is not UNSET:
            field_dict["scope_aliases"] = scope_aliases

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_import_request_scope_aliases_type_0 import KnowledgeImportRequestScopeAliasesType0

        d = dict(src_dict)
        roots = cast(list[Any], d.pop("roots"))

        source_installation_id = d.pop("source_installation_id")

        snapshot_id = d.pop("snapshot_id")

        snapshot_timestamp = d.pop("snapshot_timestamp")

        def _parse_vector_export(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        vector_export = _parse_vector_export(d.pop("vector_export", UNSET))

        def _parse_scope_aliases(data: object) -> KnowledgeImportRequestScopeAliasesType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                scope_aliases_type_0 = KnowledgeImportRequestScopeAliasesType0.from_dict(data)

                return scope_aliases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeImportRequestScopeAliasesType0 | None | Unset, data)

        scope_aliases = _parse_scope_aliases(d.pop("scope_aliases", UNSET))

        knowledge_import_request = cls(
            roots=roots,
            source_installation_id=source_installation_id,
            snapshot_id=snapshot_id,
            snapshot_timestamp=snapshot_timestamp,
            vector_export=vector_export,
            scope_aliases=scope_aliases,
        )

        knowledge_import_request.additional_properties = d
        return knowledge_import_request

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
