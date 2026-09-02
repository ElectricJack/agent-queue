from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.shadow_compile_row_dto import ShadowCompileRowDTO
    from ..models.shadow_source_error_dto import ShadowSourceErrorDTO


T = TypeVar("T", bound="PlaybookV2ShadowCompileResponse")


@_attrs_define
class PlaybookV2ShadowCompileResponse:
    """
    Attributes:
        success (bool):
        total (int):
        lowered (int):
        clean (int):
        rows (list[ShadowCompileRowDTO] | Unset):
        source_errors (list[ShadowSourceErrorDTO] | Unset):
    """

    success: bool
    total: int
    lowered: int
    clean: int
    rows: list[ShadowCompileRowDTO] | Unset = UNSET
    source_errors: list[ShadowSourceErrorDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        total = self.total

        lowered = self.lowered

        clean = self.clean

        rows: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.rows, Unset):
            rows = []
            for rows_item_data in self.rows:
                rows_item = rows_item_data.to_dict()
                rows.append(rows_item)

        source_errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.source_errors, Unset):
            source_errors = []
            for source_errors_item_data in self.source_errors:
                source_errors_item = source_errors_item_data.to_dict()
                source_errors.append(source_errors_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "total": total,
                "lowered": lowered,
                "clean": clean,
            }
        )
        if rows is not UNSET:
            field_dict["rows"] = rows
        if source_errors is not UNSET:
            field_dict["source_errors"] = source_errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.shadow_compile_row_dto import ShadowCompileRowDTO
        from ..models.shadow_source_error_dto import ShadowSourceErrorDTO

        d = dict(src_dict)
        success = d.pop("success")

        total = d.pop("total")

        lowered = d.pop("lowered")

        clean = d.pop("clean")

        _rows = d.pop("rows", UNSET)
        rows: list[ShadowCompileRowDTO] | Unset = UNSET
        if _rows is not UNSET:
            rows = []
            for rows_item_data in _rows:
                rows_item = ShadowCompileRowDTO.from_dict(rows_item_data)

                rows.append(rows_item)

        _source_errors = d.pop("source_errors", UNSET)
        source_errors: list[ShadowSourceErrorDTO] | Unset = UNSET
        if _source_errors is not UNSET:
            source_errors = []
            for source_errors_item_data in _source_errors:
                source_errors_item = ShadowSourceErrorDTO.from_dict(source_errors_item_data)

                source_errors.append(source_errors_item)

        playbook_v2_shadow_compile_response = cls(
            success=success,
            total=total,
            lowered=lowered,
            clean=clean,
            rows=rows,
            source_errors=source_errors,
        )

        return playbook_v2_shadow_compile_response
