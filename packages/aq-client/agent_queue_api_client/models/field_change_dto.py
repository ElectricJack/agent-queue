from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_value_dto import ExplanationValueDTO


T = TypeVar("T", bound="FieldChangeDTO")


@_attrs_define
class FieldChangeDTO:
    """
    Attributes:
        path (str):
        before (ExplanationValueDTO | None | Unset):
        after (ExplanationValueDTO | None | Unset):
        executable (bool | Unset):  Default: True.
    """

    path: str
    before: ExplanationValueDTO | None | Unset = UNSET
    after: ExplanationValueDTO | None | Unset = UNSET
    executable: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.explanation_value_dto import ExplanationValueDTO

        path = self.path

        before: dict[str, Any] | None | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        elif isinstance(self.before, ExplanationValueDTO):
            before = self.before.to_dict()
        else:
            before = self.before

        after: dict[str, Any] | None | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        elif isinstance(self.after, ExplanationValueDTO):
            after = self.after.to_dict()
        else:
            after = self.after

        executable = self.executable

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "path": path,
            }
        )
        if before is not UNSET:
            field_dict["before"] = before
        if after is not UNSET:
            field_dict["after"] = after
        if executable is not UNSET:
            field_dict["executable"] = executable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_value_dto import ExplanationValueDTO

        d = dict(src_dict)
        path = d.pop("path")

        def _parse_before(data: object) -> ExplanationValueDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                before_type_0 = ExplanationValueDTO.from_dict(data)

                return before_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationValueDTO | None | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        def _parse_after(data: object) -> ExplanationValueDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                after_type_0 = ExplanationValueDTO.from_dict(data)

                return after_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationValueDTO | None | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        executable = d.pop("executable", UNSET)

        field_change_dto = cls(
            path=path,
            before=before,
            after=after,
            executable=executable,
        )

        return field_change_dto
