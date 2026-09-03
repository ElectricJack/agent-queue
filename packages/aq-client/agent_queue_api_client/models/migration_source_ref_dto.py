from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="MigrationSourceRefDTO")


@_attrs_define
class MigrationSourceRefDTO:
    """Where an inventory entry's authoring Markdown lives.

    Distinct from :class:`SourceRefDTO`, which points at a span *inside* a
    source; this points at the whole file and carries its content hash.

        Attributes:
            vault_rel_path (str):
            source_sha256 (str):
            bundled_rel_path (None | str | Unset):
    """

    vault_rel_path: str
    source_sha256: str
    bundled_rel_path: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        vault_rel_path = self.vault_rel_path

        source_sha256 = self.source_sha256

        bundled_rel_path: None | str | Unset
        if isinstance(self.bundled_rel_path, Unset):
            bundled_rel_path = UNSET
        else:
            bundled_rel_path = self.bundled_rel_path

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "vault_rel_path": vault_rel_path,
                "source_sha256": source_sha256,
            }
        )
        if bundled_rel_path is not UNSET:
            field_dict["bundled_rel_path"] = bundled_rel_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        vault_rel_path = d.pop("vault_rel_path")

        source_sha256 = d.pop("source_sha256")

        def _parse_bundled_rel_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        bundled_rel_path = _parse_bundled_rel_path(d.pop("bundled_rel_path", UNSET))

        migration_source_ref_dto = cls(
            vault_rel_path=vault_rel_path,
            source_sha256=source_sha256,
            bundled_rel_path=bundled_rel_path,
        )

        return migration_source_ref_dto
