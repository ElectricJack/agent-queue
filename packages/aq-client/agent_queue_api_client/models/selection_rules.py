from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.path_areas import PathAreas
    from ..models.scoped_conftest import ScopedConftest
    from ..models.source_scan import SourceScan


T = TypeVar("T", bound="SelectionRules")


@_attrs_define
class SelectionRules:
    """
    Attributes:
        version (Literal[1] | Unset):  Default: 1.
        global_invalidators (list[str] | Unset):
        scoped_conftests (list[ScopedConftest] | Unset):
        non_behavioral (list[str] | Unset):
        ownership (list[PathAreas] | Unset):
        source_scanning (list[SourceScan] | Unset):
        critical (list[str] | Unset):
    """

    version: Literal[1] | Unset = 1
    global_invalidators: list[str] | Unset = UNSET
    scoped_conftests: list[ScopedConftest] | Unset = UNSET
    non_behavioral: list[str] | Unset = UNSET
    ownership: list[PathAreas] | Unset = UNSET
    source_scanning: list[SourceScan] | Unset = UNSET
    critical: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        version = self.version

        global_invalidators: list[str] | Unset = UNSET
        if not isinstance(self.global_invalidators, Unset):
            global_invalidators = self.global_invalidators

        scoped_conftests: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.scoped_conftests, Unset):
            scoped_conftests = []
            for scoped_conftests_item_data in self.scoped_conftests:
                scoped_conftests_item = scoped_conftests_item_data.to_dict()
                scoped_conftests.append(scoped_conftests_item)

        non_behavioral: list[str] | Unset = UNSET
        if not isinstance(self.non_behavioral, Unset):
            non_behavioral = self.non_behavioral

        ownership: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.ownership, Unset):
            ownership = []
            for ownership_item_data in self.ownership:
                ownership_item = ownership_item_data.to_dict()
                ownership.append(ownership_item)

        source_scanning: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.source_scanning, Unset):
            source_scanning = []
            for source_scanning_item_data in self.source_scanning:
                source_scanning_item = source_scanning_item_data.to_dict()
                source_scanning.append(source_scanning_item)

        critical: list[str] | Unset = UNSET
        if not isinstance(self.critical, Unset):
            critical = self.critical

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if version is not UNSET:
            field_dict["version"] = version
        if global_invalidators is not UNSET:
            field_dict["global_invalidators"] = global_invalidators
        if scoped_conftests is not UNSET:
            field_dict["scoped_conftests"] = scoped_conftests
        if non_behavioral is not UNSET:
            field_dict["non_behavioral"] = non_behavioral
        if ownership is not UNSET:
            field_dict["ownership"] = ownership
        if source_scanning is not UNSET:
            field_dict["source_scanning"] = source_scanning
        if critical is not UNSET:
            field_dict["critical"] = critical

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.path_areas import PathAreas
        from ..models.scoped_conftest import ScopedConftest
        from ..models.source_scan import SourceScan

        d = dict(src_dict)
        version = cast(Literal[1] | Unset, d.pop("version", UNSET))
        if version != 1 and not isinstance(version, Unset):
            raise ValueError(f"version must match const 1, got '{version}'")

        global_invalidators = cast(list[str], d.pop("global_invalidators", UNSET))

        _scoped_conftests = d.pop("scoped_conftests", UNSET)
        scoped_conftests: list[ScopedConftest] | Unset = UNSET
        if _scoped_conftests is not UNSET:
            scoped_conftests = []
            for scoped_conftests_item_data in _scoped_conftests:
                scoped_conftests_item = ScopedConftest.from_dict(scoped_conftests_item_data)

                scoped_conftests.append(scoped_conftests_item)

        non_behavioral = cast(list[str], d.pop("non_behavioral", UNSET))

        _ownership = d.pop("ownership", UNSET)
        ownership: list[PathAreas] | Unset = UNSET
        if _ownership is not UNSET:
            ownership = []
            for ownership_item_data in _ownership:
                ownership_item = PathAreas.from_dict(ownership_item_data)

                ownership.append(ownership_item)

        _source_scanning = d.pop("source_scanning", UNSET)
        source_scanning: list[SourceScan] | Unset = UNSET
        if _source_scanning is not UNSET:
            source_scanning = []
            for source_scanning_item_data in _source_scanning:
                source_scanning_item = SourceScan.from_dict(source_scanning_item_data)

                source_scanning.append(source_scanning_item)

        critical = cast(list[str], d.pop("critical", UNSET))

        selection_rules = cls(
            version=version,
            global_invalidators=global_invalidators,
            scoped_conftests=scoped_conftests,
            non_behavioral=non_behavioral,
            ownership=ownership,
            source_scanning=source_scanning,
            critical=critical,
        )

        return selection_rules
