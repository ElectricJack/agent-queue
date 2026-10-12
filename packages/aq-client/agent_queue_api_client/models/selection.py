from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.selection_scope import SelectionScope
from ..types import UNSET, Unset

T = TypeVar("T", bound="Selection")


@_attrs_define
class Selection:
    """
    Attributes:
        scope (SelectionScope | Unset):  Default: SelectionScope.PROJECT.
        overwrite (bool | Unset):  Default: False.
        expected_checksum (None | str | Unset):
    """

    scope: SelectionScope | Unset = SelectionScope.PROJECT
    overwrite: bool | Unset = False
    expected_checksum: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        scope: str | Unset = UNSET
        if not isinstance(self.scope, Unset):
            scope = self.scope.value

        overwrite = self.overwrite

        expected_checksum: None | str | Unset
        if isinstance(self.expected_checksum, Unset):
            expected_checksum = UNSET
        else:
            expected_checksum = self.expected_checksum

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if scope is not UNSET:
            field_dict["scope"] = scope
        if overwrite is not UNSET:
            field_dict["overwrite"] = overwrite
        if expected_checksum is not UNSET:
            field_dict["expected_checksum"] = expected_checksum

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _scope = d.pop("scope", UNSET)
        scope: SelectionScope | Unset
        if isinstance(_scope, Unset):
            scope = UNSET
        else:
            scope = SelectionScope(_scope)

        overwrite = d.pop("overwrite", UNSET)

        def _parse_expected_checksum(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_checksum = _parse_expected_checksum(d.pop("expected_checksum", UNSET))

        selection = cls(
            scope=scope,
            overwrite=overwrite,
            expected_checksum=expected_checksum,
        )

        return selection
